import json
import logging
import sys
import typing
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyproj
import pyvista as pv
import rasterio as rio
import skimage
import torch
from IPython.core.debugger import set_trace
from pytorch3d.renderer import (
    AmbientLights,
    HardGouraudShader,
    MeshRasterizer,
    RasterizationSettings,
    TexturesVertex,
)
from pytorch3d.structures import Meshes
from shapely import MultiPolygon, Point, Polygon
from skimage.transform import resize
from sklearn.cluster import KMeans
from tqdm import tqdm

from multiview_mapping_toolkit.cameras import (
    PhotogrammetryCamera,
    PhotogrammetryCameraSet,
)
from multiview_mapping_toolkit.constants import (
    CLASS_ID_KEY,
    CLASS_NAMES_KEY,
    EARTH_CENTERED_EARTH_FIXED_EPSG_CODE,
    LAT_LON_EPSG_CODE,
    NULL_TEXTURE_FLOAT_VALUE,
    NULL_TEXTURE_INT_VALUE,
    PATH_TYPE,
    TEN_CLASS_VIS_KWARGS,
    TWENTY_CLASS_VIS_KWARGS,
    VERT_ID,
    VIS_FOLDER,
)
from multiview_mapping_toolkit.utils.geometric import batched_unary_union
from multiview_mapping_toolkit.utils.geospatial import (
    ensure_geometric_CRS,
    ensure_non_overlapping_polygons,
    get_projected_CRS,
)
from multiview_mapping_toolkit.utils.indexing import ensure_float_labels
from multiview_mapping_toolkit.utils.numeric import compute_3D_triangle_area
from multiview_mapping_toolkit.utils.parsing import parse_transform_metashape


class TexturedPhotogrammetryMesh:
    def __init__(
        self,
        mesh: typing.Union[PATH_TYPE, pv.PolyData],
        downsample_target: float = 1.0,
        transform_filename: PATH_TYPE = None,
        texture: typing.Union[PATH_TYPE, np.ndarray, None] = None,
        texture_column_name: typing.Union[PATH_TYPE, None] = None,
        IDs_to_labels: typing.Union[None, dict] = None,
        ROI=None,
        ROI_buffer_meters: float = 0,
        require_transform: bool = False,
        log_level: str = "INFO",
    ):
        """_summary_

        Args:
            mesh (typing.Union[PATH_TYPE, pv.PolyData]): Path to the mesh, in a format pyvista can read, or pyvista mesh
            downsample_target (float, optional): Downsample to this fraction of vertices. Defaults to 1.0.
            texture (typing.Union[PATH_TYPE, np.ndarray, None]): Texture or path to one. See more details in `load_texture` documentation
            texture_column_name: The name of the column to use for a vectorfile input
            IDs_to_labels (typing.Union[None, dict]): Dictionary mapping from integer IDs to string class names
        """
        self.downsample_target = downsample_target

        self.pyvista_mesh = None
        self.pytorch3d_mesh = None
        self.texture = None
        self.vertex_texture = None
        self.face_texture = None
        self.local_to_epgs_4978_transform = None
        self.IDs_to_labels = None

        self.logger = logging.getLogger(f"mesh_{id(self)}")
        self.logger.setLevel(log_level)
        # Potentially necessary for Jupyter
        # https://stackoverflow.com/questions/35936086/jupyter-notebook-does-not-print-logs-to-the-output-cell
        self.logger.addHandler(logging.StreamHandler(stream=sys.stdout))

        if torch.cuda.is_available():
            self.device = torch.device("cuda:0")
            torch.cuda.set_device(self.device)
        else:
            self.device = torch.device("cpu")

        # Load the transform
        self.logger.info("Loading transform to EPSG:4326")
        self.load_transform_to_epsg_4326(
            transform_filename, require_transform=require_transform
        )
        # Load the mesh with the pyvista loader
        self.logger.info("Loading mesh")
        self.load_mesh(
            mesh=mesh,
            downsample_target=downsample_target,
            ROI=ROI,
            ROI_buffer_meters=ROI_buffer_meters,
        )
        # Load the texture
        self.logger.info("Loading texture")
        self.load_texture(texture, texture_column_name, IDs_to_labels=IDs_to_labels)

    # Setup methods

    def load_mesh(
        self,
        mesh: typing.Union[PATH_TYPE, pv.PolyData],
        downsample_target: float = 1.0,
        ROI=None,
        ROI_buffer_meters=0,
    ):
        """Load the pyvista mesh and create the pytorch3d texture

        Args:
            mesh (typing.Union[PATH_TYPE, pv.PolyData]):
                Path to the mesh or actual mesh
            downsample_target (float, optional):
                What fraction of mesh vertices to downsample to. Defaults to 1.0, (does nothing).
        """
        if isinstance(mesh, pv.PolyData):
            self.pyvista_mesh = mesh
        else:
            # Load the mesh using pyvista
            # TODO see if pytorch3d has faster/more flexible readers. I'd assume no, but it's good to check
            self.logger.info("Reading the mesh")
            self.pyvista_mesh = pv.read(mesh)

        self.logger.info("Selecting an ROI from mesh")
        # Select a region of interest if needed
        self.pyvista_mesh = self.select_mesh_ROI(
            region_of_interest=ROI, buffer_meters=ROI_buffer_meters
        )

        # Downsample mesh if needed
        if downsample_target != 1.0:
            # TODO try decimate_pro and compare quality and runtime
            # TODO see if there's a way to preserve the mesh colors
            # TODO also see this decimation algorithm: https://pyvista.github.io/fast-simplification/
            self.logger.info("Downsampling the mesh")
            self.pyvista_mesh = self.pyvista_mesh.decimate(
                target_reduction=(1 - downsample_target)
            )
        self.logger.info("Extracting faces from mesh")
        # See here for format: https://github.com/pyvista/pyvista-support/issues/96
        self.faces = self.pyvista_mesh.faces.reshape((-1, 4))[:, 1:4].copy()

    def load_transform_to_epsg_4326(
        self, transform_filename: PATH_TYPE, require_transform: bool = False
    ):
        """
        Load the 4x4 transform projects points from their local coordnate system into EPSG:4326,
        the earth-centered, earth-fixed coordinate frame. This can either be from a CSV file specifying
        it directly or extracted from a Metashape camera output

        Args
            transform_filename (PATH_TYPE):
            require_transform (bool): Does a local-to-global transform file need to be available"
        Raises:
            FileNotFoundError: Cannot find texture file
            ValueError: Transform file doesn't have 4x4 matrix
        """
        if transform_filename is None:
            if require_transform:
                raise ValueError("Transform is required but not provided")
            # If not required, do nothing. TODO consider adding a warning
            return

        elif Path(transform_filename).suffix == ".xml":
            self.local_to_epgs_4978_transform = parse_transform_metashape(
                transform_filename
            )
        elif Path(transform_filename).suffix == ".csv":
            self.local_to_epgs_4978_transform = np.loadtxt(
                transform_filename, delimiter=","
            )
            if self.local_to_epgs_4978_transform.shape != (4, 4):
                raise ValueError(
                    f"Transform should be (4,4) but is {self.local_to_epgs_4978_transform.shape}"
                )
        else:
            if require_transform:
                raise ValueError(
                    f"Transform could not be loaded from {transform_filename}"
                )
            # Not set
            return

    def standardize_texture(self, texture_array: np.ndarray):
        # TODO consider coercing into a numpy array

        # Check the dimensions
        if texture_array.ndim == 1:
            texture_array = np.expand_dims(texture_array, axis=1)
        elif texture_array.ndim != 2:
            raise ValueError(
                f"Input texture should have 1 or 2 dimensions but instead has {texture_array.ndim}"
            )
        return texture_array

    def get_texture(
        self,
        request_vertex_texture: typing.Union[bool, None] = None,
        try_verts_faces_conversion: bool = True,
    ):
        if self.vertex_texture is None and self.face_texture is None:
            return

        # If this is unset, try to infer it
        if request_vertex_texture is None:
            if self.vertex_texture is not None and self.face_texture is not None:
                raise ValueError(
                    "Ambigious which texture is requested, set request_vertex_texture appropriately"
                )

            # Assume that the only one available is being requested
            request_vertex_texture = self.vertex_texture is not None

        if request_vertex_texture:
            if self.vertex_texture is not None:
                return self.standardize_texture(self.vertex_texture)
            elif try_verts_faces_conversion:
                self.set_texture(self.face_to_vert_texture(self.face_texture))
                self.vertex_texture
            else:
                raise ValueError(
                    "Vertex texture not present and conversion was not requested"
                )
        else:
            if self.face_texture is not None:
                return self.standardize_texture(self.face_texture)
            elif try_verts_faces_conversion:
                self.set_texture(self.vert_to_face_texture(self.vertex_texture))
                return self.face_texture
            else:
                raise ValueError(
                    "Face texture not present and conversion was not requested"
                )

    def is_discrete_texture(self):
        return self.IDs_to_labels is not None

    def set_texture(
        self,
        texture_array: np.ndarray,
        IDs_to_labels: typing.Union[None, dict] = None,
        all_discrete_texture_values: typing.Union[typing.List, None] = None,
        is_vertex_texture: typing.Union[bool, None] = None,
        use_derived_IDs_to_labels: bool = False,
        delete_existing: bool = True,
    ):
        """Set the internal texture representation

        Args:
            texture_array (np.ndarray):
                The array of texture values. The first dimension must be the length of faces or verts. A second dimension is optional.
            IDs_to_labels (typing.Union[None, dict], optional): Mapping from integer IDs to string names. Defaults to None.
            all_discrete_texture_values (typing.Union[typing.List, None], optional):
                Are all the texture values known to be discrete, representing IDs. Computed from the data if not set. Defaults to None.
            is_vertex_texture (typing.Union[bool, None], optional):
                Are the texture values supposed to correspond to the vertices. Computed from the data if not set. Defaults to None.
            use_derived_IDs_to_labels (bool, optional): Use IDs to labels derived from data if not explicitly provided. Defaults to False.
            delete_existing (bool, optional): Delete the existing texture when the other one (face, vertex) is set. Defaults to True.

        Raises:
            ValueError: If the size of the texture doesn't match the number of either faces or vertices
            ValueError: If the number of faces and vertices are the same and is_vertex_texture isn't set
        """
        texture_array = self.standardize_texture(texture_array)
        # IDs_to_labels (typing.Union[None, dict]): Dictionary mapping from integer IDs to string class names

        # If it is not specified whether this is a vertex texture, attempt to infer it from the shape
        # TODO consider refactoring to check whether it matches the number of one of them,
        # no matter whether is_vertex_texture is specified
        if is_vertex_texture is None:
            # Check that the number of matches face or verts
            n_values = texture_array.shape[0]
            n_faces = self.faces.shape[0]
            n_verts = self.pyvista_mesh.points.shape[0]

            if n_verts == n_faces:
                raise ValueError(
                    "Cannot infer whether texture should be applied to vertices of faces because the number is the same"
                )
            elif n_values == n_verts:
                is_vertex_texture = True
            elif n_values == n_faces:
                is_vertex_texture = False
            else:
                raise ValueError(
                    f"The number of elements in the texture ({n_values}) did not match the number of faces ({n_faces}) or vertices ({n_verts})"
                )

        # Ensure that the actual data type is float, and record label names
        if texture_array.ndim == 2 and texture_array.shape[1] != 1:
            # If it is more than one column, it's assumed to be a real-valued
            # quantity and we try to cast it to a float
            texture_array = texture_array.astype(float)
            derived_IDs_to_labels = None
        else:
            texture_array, derived_IDs_to_labels = ensure_float_labels(
                texture_array, full_array=all_discrete_texture_values
            )

        # If IDs to labels is explicitly provided, trust that
        if IDs_to_labels is not None:
            # TODO should do some type checking here
            self.IDs_to_labels = IDs_to_labels
        # If not, but we can compute it, use that. Otherwise, we might want to force them to be set to None
        elif use_derived_IDs_to_labels:
            self.IDs_to_labels = derived_IDs_to_labels

        # Set the appropriate texture and optionally delete the other one
        if is_vertex_texture:
            self.vertex_texture = texture_array
            if delete_existing:
                self.face_texture = None
        else:
            self.face_texture = texture_array
            if delete_existing:
                self.vertex_texture = None

    def load_texture(
        self,
        texture: typing.Union[str, PATH_TYPE, np.ndarray, None],
        texture_column_name: typing.Union[None, PATH_TYPE] = None,
        IDs_to_labels: typing.Union[None, dict] = None,
    ):
        """Sets either self.face_texture or self.vertex_texture to an (n_{faces, verts}, m channels) array. Note that the other
           one will be left as None

        Args:
            texture (typing.Union[PATH_TYPE, np.ndarray, None]): This is either a numpy array or a file to one of the following
                * A numpy array file in ".npy" format
                * A vector file readable by geopandas and a label(s) specifying which column to use.
                  This should be dataset of polygons/multipolygons. Ideally, there should be no overlap between
                  regions with different labels. These regions may be assigned based on the order of the rows.
                * A raster file readable by rasterio. We may want to support using a subset of bands
            texture_column_name: The column to use as the label for a vector data input
            IDs_to_labels (typing.Union[None, dict]): Dictionary mapping from integer IDs to string class names
        """
        # The easy case, a texture is passed in directly
        if isinstance(texture, np.ndarray):
            self.set_texture(
                texture_array=texture,
                IDs_to_labels=IDs_to_labels,
                use_derived_IDs_to_labels=True,
            )
        # If the texture is None, try to load it from the mesh
        # Note that this requires us to have not decimated yet
        elif texture is None:
            # See if the mesh has a texture, else this will be None
            texture_array = self.pyvista_mesh.active_scalars

            if texture_array is not None:
                # Check if this was a really one channel that had to be tiled to
                # three for saving
                if len(texture_array.shape) == 2:
                    min_val_per_row = np.min(texture_array, axis=1)
                    max_val_per_row = np.max(texture_array, axis=1)
                    if np.array_equal(min_val_per_row, max_val_per_row):
                        # This is supposted to be one channel
                        texture_array = texture_array[:, 0].astype(float)
                        # Set any values that are the ignore int value to nan
                texture_array = texture_array.astype(float)
                texture_array[texture_array == NULL_TEXTURE_INT_VALUE] = np.nan

                self.set_texture(
                    texture_array,
                    IDs_to_labels=IDs_to_labels,
                    use_derived_IDs_to_labels=True,
                )
            else:
                if IDs_to_labels is not None:
                    self.IDs_to_labels = IDs_to_labels
                # Assume that no texture will be needed, consider printing a warning
                self.logger.warn("No texture provided")
        else:
            # Try handling all the other supported filetypes
            texture_array = None
            all_values = None

            # Name of scalar in the mesh
            try:
                texture_array = self.pyvista_mesh[texture]
            except (KeyError, ValueError):
                self.logger.warn(
                    "Could not read texture as a scalar from the pyvista mesh"
                )

            # Numpy file
            if texture_array is None:
                try:
                    texture_array = np.load(texture, allow_pickle=True)
                except:
                    self.logger.warn("Could not read texture as a numpy file")

            # Vector file
            if texture_array is None:
                try:
                    # TODO IDs to labels should be used here if set so the computed IDs are aligned with that mapping
                    texture_array, all_values = self.get_values_for_verts_from_vector(
                        column_names=texture_column_name,
                        vector_source=texture,
                    )
                except IndexError:
                    self.logger.warn("Could not read texture as vector file")

            # Raster file
            if texture_array is None:
                try:
                    # TODO
                    texture_array = self.get_vert_values_from_raster_file(texture)
                except:
                    self.logger.warn("Could not read texture as raster file")

            # Error out if not set, since we assume the intent was to have a texture at this point
            if texture_array is None:
                raise ValueError(f"Could not load texture for {texture}")

            # This will error if something is wrong with the texture that was loaded
            self.set_texture(
                texture_array,
                all_discrete_texture_values=all_values,
                use_derived_IDs_to_labels=True,
                IDs_to_labels=IDs_to_labels,
            )

    def select_mesh_ROI(
        self,
        region_of_interest: typing.Union[
            gpd.GeoDataFrame, Polygon, MultiPolygon, PATH_TYPE, None
        ],
        buffer_meters: float = 0,
        default_CRS: pyproj.CRS = pyproj.CRS.from_epsg(4326),
        return_original_IDs: bool = False,
    ):
        """Get a subset of the mesh based on geospatial data

        Args:
            region_of_interest (typing.Union[gpd.GeoDataFrame, Polygon, MultiPolygon, PATH_TYPE]):
                Region of interest. Can be a
                * dataframe, where all columns will be colapsed
                * A shapely polygon/multipolygon
                * A file that can be loaded by geopandas
            buffer_meters (float, optional): Expand the geometry by this amount of meters. Defaults to 0.
            default_CRS (pyproj.CRS, optional): The CRS to use if one isn't provided. Defaults to pyproj.CRS.from_epsg(4326).
            return_original_IDs (bool, optional): Return the indices into the original mesh. Defaults to False.

        Returns:
            pyvista.PolyData: The subset of the mesh
            np.ndarray: The indices of the points in the original mesh (only if return_original_IDs set)
            np.ndarray: The indices of the faces in the original mesh (only if return_original_IDs set)
        """
        if region_of_interest is None:
            return self.pyvista_mesh

        # Get the ROI into a geopandas GeoDataFrame
        self.logger.info("Standardizing ROI")
        if isinstance(region_of_interest, gpd.GeoDataFrame):
            ROI_gpd = region_of_interest
        elif isinstance(region_of_interest, (Polygon, MultiPolygon)):
            ROI_gpd = gpd.DataFrame(crs=default_CRS, geometry=[region_of_interest])
        else:
            ROI_gpd = gpd.read_file(region_of_interest)

        self.logger.info("Dissolving ROI")
        # Disolve to ensure there is only one row
        ROI_gpd = ROI_gpd.dissolve()
        self.logger.info("Setting CRS and buffering ROI")
        # Make sure we're using a geometric CRS so a buffer can be applied
        ROI_gpd = ensure_geometric_CRS(ROI_gpd)
        # Apply the buffer
        ROI_gpd["geometry"] = ROI_gpd.buffer(buffer_meters)
        self.logger.info("Dissolving buffered ROI")
        # Disolve again in case
        ROI_gpd = ROI_gpd.dissolve()

        self.logger.info("Extracting verts for dataframe")
        # Get the vertices as a dataframe in the same CRS
        verts_df = self.get_verts_geodataframe(ROI_gpd.crs)
        self.logger.info("Checking intersection of verts with ROI")
        # Determine which vertices are within the ROI polygon
        verts_in_ROI = gpd.tools.overlay(verts_df, ROI_gpd, how="intersection")
        # Extract the IDs of the set within the polygon
        vert_inds = verts_in_ROI["vert_ID"].to_numpy()

        self.logger.info("Extracting points from pyvista mesh")
        # Extract a submesh using these IDs, which is returned as an UnstructuredGrid
        subset_unstructured_grid = self.pyvista_mesh.extract_points(vert_inds)
        self.logger.info("Extraction surface from subset mesh")
        # Convert the unstructured grid to a PolyData (mesh) again
        subset_mesh = subset_unstructured_grid.extract_surface()

        # If we need the indices into the original mesh, return those
        if return_original_IDs:
            return (
                subset_mesh,
                subset_unstructured_grid["vtkOriginalPointIds"],
                subset_unstructured_grid["vtkOriginalCellIds"],
            )
        # Else return just the mesh
        return subset_mesh

    def add_label(self, label_name, label_ID):
        if label_ID is not np.nan:
            self.IDs_to_labels[label_ID] = label_name

    def get_IDs_to_labels(self):
        return self.IDs_to_labels

    def get_label_names(self):
        self.logger.warning(
            "This method will be deprecated in favor of get_IDs_to_labels since it doesn't handle non-sequential indices"
        )
        if self.IDs_to_labels is None:
            return None
        return list(self.IDs_to_labels.values())

    def create_pytorch3d_mesh(
        self,
        vert_texture: np.ndarray = None,
        force_recreation: bool = False,
        batch_size: int = 1,
    ):
        """Create the pytorch_3d_mesh

        Args:
            vert_texture (np.ndarray, optional):
                Optional texture, (n_verts, n_channels). In the range [0, 1]. Defaults to None.
            force_recreation (bool, optional):
                if True, create a new mesh even if one already exists
        """
        # No-op if a mesh exists already
        if not force_recreation and self.pytorch3d_mesh is not None:
            return

        # Create the texture object if provided
        if vert_texture is not None:
            vert_texture = (
                torch.Tensor(vert_texture).to(torch.float).to(self.device).unsqueeze(0)
            )
            if len(vert_texture.shape) == 2:
                vert_texture = vert_texture.unsqueeze(-1)
            texture = TexturesVertex(verts_features=vert_texture).to(self.device)
        else:
            texture = None

        # Create the pytorch mesh
        self.pytorch3d_mesh = Meshes(
            verts=[torch.Tensor(self.pyvista_mesh.points).to(self.device)],
            faces=[torch.Tensor(self.faces).to(self.device)],
            textures=texture,
        ).to(self.device)

        if batch_size != 1 and len(self.pytorch3d_mesh) == 1:
            self.pytorch3d_mesh = self.pytorch3d_mesh.extend(batch_size)

    # Vertex methods

    def transform_vertices(self, transform_4x4: np.ndarray, in_place: bool = False):
        """Apply a transform to the vertex coordinates

        Args:
            transform_4x4 (np.ndarray): Transform to be applied
            in_place (bool): Should the vertices be updated for all member objects
        """
        homogenous_local_points = np.vstack(
            (self.pyvista_mesh.points.T, np.ones(self.pyvista_mesh.points.shape[0]))
        )
        transformed_local_points = transform_4x4 @ homogenous_local_points
        transformed_local_points = transformed_local_points[:3].T

        # Overwrite existing vertices in both pytorch3d and pyvista mesh
        if in_place:
            self.pyvista_mesh.points = transformed_local_points.copy()
        return transformed_local_points

    def get_vertices_in_CRS(
        self, output_CRS: pyproj.CRS, force_easting_northing: bool = True
    ):
        """Return the coordinates of the mesh vertices in a given CRS

        Args:
            output_CRS (pyproj.CRS): The coordinate reference system to transform to
            force_easting_northing (bool, optional): Ensure that the returned points are east first, then north

        Returns:
            np.ndarray: (n_points, 3)
        """
        # If no CRS is requested, just return the points
        if output_CRS is None:
            return self.pyvista_mesh.points

        # The mesh points are defined in an arbitrary local coordinate system but we can transform them to EPGS:4978,
        # the earth-centered, earth-fixed coordinate system, using an included transform
        epgs4978_verts = self.transform_vertices(self.local_to_epgs_4978_transform)

        # TODO figure out why this conversion was required. I think it was some typing issue
        output_CRS = pyproj.CRS.from_epsg(output_CRS.to_epsg())
        # Build a pyproj transfrormer from EPGS:4978 to the desired CRS
        transformer = pyproj.Transformer.from_crs(
            EARTH_CENTERED_EARTH_FIXED_EPSG_CODE, output_CRS
        )

        # Transform the coordinates
        verts_in_output_CRS = transformer.transform(
            xx=epgs4978_verts[:, 0],
            yy=epgs4978_verts[:, 1],
            zz=epgs4978_verts[:, 2],
        )
        # Stack and transpose
        verts_in_output_CRS = np.vstack(verts_in_output_CRS).T

        # Pyproj respects the CRS axis ordering, which is northing/easting for most projected coordinate systems
        # This causes headaches because it's assumed by rasterio and geopandas to be easting/northing
        # https://rasterio.readthedocs.io/en/stable/api/rasterio.crs.html#rasterio.crs.epsg_treats_as_latlong
        if force_easting_northing and rio.crs.epsg_treats_as_latlong(output_CRS):
            # Swap first two columns
            verts_in_output_CRS = verts_in_output_CRS[:, [1, 0, 2]]

        return verts_in_output_CRS

    def get_verts_geodataframe(self, crs: pyproj.CRS) -> gpd.GeoDataFrame:
        """Obtain the vertices as a dataframe

        Args:
            crs (pyproj.CRS): The CRS to use

        Returns:
            gpd.GeoDataFrame: A dataframe with all the vertices
        """
        # Get the vertices in the same CRS as the geofile
        verts_in_geopolygon_crs = self.get_vertices_in_CRS(crs)

        df = pd.DataFrame(
            {
                "east": verts_in_geopolygon_crs[:, 0],
                "north": verts_in_geopolygon_crs[:, 1],
            }
        )
        # Create a column of Point objects to use as the geometry
        df["geometry"] = gpd.points_from_xy(df["east"], df["north"])
        points = gpd.GeoDataFrame(df, crs=crs)

        # Add an index column because the normal index will not be preserved in future operations
        points[VERT_ID] = df.index

        return points

    # Transform labels face<->vertex methods

    def face_to_vert_texture(self, face_IDs):
        """_summary_

        Args:
            face_IDs (np.array): (n_faces,) The integer IDs of the faces
        """
        raise NotImplementedError()
        # TODO figure how to have a NaN class that
        for i in tqdm(range(self.pyvista_mesh.points.shape[0])):
            # Find which faces are using this vertex
            matching = np.sum(self.faces == i, axis=1)
            # matching_inds = np.where(matching)[0]
            # matching_IDs = face_IDs[matching_inds]
            # most_common_ind = Counter(matching_IDs).most_common(1)

    def vert_to_face_texture(self, vert_IDs):
        if vert_IDs is None:
            raise ValueError("None")

        vert_IDs = np.squeeze(vert_IDs)
        if vert_IDs.ndim != 1:
            raise ValueError(
                f"Can only perform conversion with one dimensional array but instead had {vert_IDs.ndim}"
            )

        # Each row contains the IDs of each vertex
        IDs_per_face = vert_IDs[self.faces]
        # Now we need to "vote" for the best one
        max_ID = int(np.nanmax(vert_IDs))
        # TODO consider using unique if these indices are sparse
        counts_per_class_per_face = np.array(
            [np.sum(IDs_per_face == i, axis=1) for i in range(max_ID + 1)]
        ).T
        # Check which entires had no classes reported and mask them out
        # TODO consider removing these rows beforehand
        zeros_mask = np.all(counts_per_class_per_face == 0, axis=1)
        # We want to fairly tiebreak since np.argmax will always take th first index
        # This is hard to do in a vectorized way, so we just add a small random value
        # independently to each element
        counts_per_class_per_face = (
            counts_per_class_per_face
            + np.random.random(counts_per_class_per_face.shape) * 0.5
        )
        most_common_class_per_face = np.argmax(counts_per_class_per_face, axis=1)
        # Set any faces with zero counts to the null value
        most_common_class_per_face[zeros_mask] = NULL_TEXTURE_FLOAT_VALUE

        return most_common_class_per_face

    # Operations on vector data
    def get_values_for_verts_from_vector(
        self,
        vector_source: typing.Union[gpd.GeoDataFrame, PATH_TYPE],
        column_names: typing.Union[str, typing.List[str]],
    ) -> np.ndarray:
        """Get the value from a dataframe for each vertex

        Args:
            vector_source (typing.Union[gpd.GeoDataFrame, PATH_TYPE]): geo data frame or path to data that can be loaded by geopandas
            column_names (typing.Union[str, typing.List[str]]): Which columns to obtain data from

        Returns:
            np.ndarray: Array of values for each vertex if there is one column name or
            dict[np.ndarray]: A dict mapping from column names to numpy arrays
        """
        # Lead the vector data if not already provided in memory
        if isinstance(vector_source, gpd.GeoDataFrame):
            gdf = vector_source
        else:
            # This will error if not readable
            gdf = gpd.read_file(vector_source)

        # Infer or standardize the column names
        if column_names is None:
            # Check if there is only one real column
            if len(gdf.columns) == 2:
                column_names = list(filter(lambda x: x != "geometry", gdf.columns))
            else:
                # Log as well since this may be caught by an exception handler,
                # and it's a user error that can be corrected
                self.logger.error(
                    "No column name provided and ambigious which column to use"
                )
                raise ValueError(
                    "No column name provided and ambigious which column to use"
                )
        # If only one column is provided, make it a one-length list
        elif isinstance(column_names, str):
            column_names = [column_names]

        # Get a dataframe of vertices
        verts_df = self.get_verts_geodataframe(gdf.crs)

        # See which vertices are in the geopolygons
        points_in_polygons_gdf = gpd.tools.overlay(verts_df, gdf, how="intersection")
        # Get the index array
        index_array = points_in_polygons_gdf[VERT_ID].to_numpy()

        # This is one entry per vertex
        labeled_verts_dict = {}
        all_values_dict = {}
        # Extract the data from each
        for column_name in column_names:
            # Create an array corresponding to all the points and initialize to NaN
            column_values = points_in_polygons_gdf[column_name]
            # TODO clean this up
            if column_values.dtype == str or column_values.dtype == np.dtype("O"):
                # TODO be set to the default value for the type of the column
                null_value = "null"
            elif column_values.dtype == int:
                null_value = 255
            else:
                null_value = np.nan
            # Create an array, one per vertex, with the null value
            values = np.full(
                shape=verts_df.shape[0],
                dtype=column_values.dtype,
                fill_value=null_value,
            )
            # Assign the labeled values
            values[index_array] = column_values

            # Record the results
            labeled_verts_dict[column_name] = values
            all_values_dict[column_name] = gdf[column_name]

        # If only one name was requested, just return that
        if len(column_names) == 1:
            labeled_verts = np.array(list(labeled_verts_dict.values())[0])
            all_values = np.array(list(all_values_dict.values())[0])

            return labeled_verts, all_values
        # Else return a dict of all requested values
        return labeled_verts_dict, all_values_dict

    def save_mesh(self, savepath: PATH_TYPE, save_vert_texture: bool = True):
        # TODO consider moving most of this functionality to a utils file
        if save_vert_texture:
            vert_texture = self.get_texture(request_vertex_texture=True)
            n_channels = vert_texture.shape[1]

            if n_channels == 1:
                vert_texture = np.nan_to_num(vert_texture, nan=NULL_TEXTURE_INT_VALUE)
                vert_texture = np.tile(vert_texture, reps=(1, 3))
            if n_channels > 3:
                self.logger.warning(
                    "Too many channels to save, attempting to treat them as class probabilities and take the argmax"
                )
                # Take the argmax
                vert_texture = np.nanargmax(vert_texture, axis=1, keepdims=True)
                # Replace nan with 255
                vert_texture = np.nan_to_num(vert_texture, nan=NULL_TEXTURE_INT_VALUE)
                # Expand to the right number of channels
                vert_texture = np.repeat(vert_texture, repeats=(1, 3))

            vert_texture = vert_texture.astype(np.uint8)
        else:
            vert_texture = None

        # Create folder if it doesn't exist
        Path(savepath).parent.mkdir(parents=True, exist_ok=True)
        # Actually save the mesh
        self.pyvista_mesh.save(savepath, texture=vert_texture)

    def export_face_labels_vector(
        self,
        face_labels: typing.Union[np.ndarray, None] = None,
        export_file: PATH_TYPE = None,
        export_crs: pyproj.CRS = LAT_LON_EPSG_CODE,
        label_names: typing.Tuple = None,
        ensure_non_overlapping: bool = False,
        simplify_tol: float = 0.0,
        drop_nan: bool = True,
        vis: bool = True,
        batched_unary_union_kwargs: typing.Dict = {
            "batch_size": 500000,
            "sort_by_loc": True,
            "grid_size": 0.05,
            "simplify_tol": 0.05,
        },
        vis_kwargs: typing.Dict = {},
    ) -> gpd.GeoDataFrame:
        """Export the labels for each face as a on-per-class multipolygon

        Args:
            face_labels (np.ndarray): Array of integer labels and potentially nan
            export_file (PATH_TYPE, optional):
                Where to export. The extension must be a filetype that geopandas can write.
                Defaults to None, if unset, nothing will be written.
            export_crs (pyproj.CRS, optional): What CRS to export in.. Defaults to pyproj.CRS.from_epsg(4326), lat lon.
            label_names (typing.Tuple, optional): Optional names, that are indexed by the labels. Defaults to None.
            ensure_non_overlapping (bool, optional): Should regions where two classes are predicted at different z heights be assigned to one class
            simplify_tol: (float, optional): Tolerence in meters to use to simplify geometry
            drop_nan (bool, optional): Don't export the nan class, often used for background
            vis: should the result be visualzed
            batched_unary_union_kwargs (dict, optional): Keyword arguments for batched_unary_union_call
            vis_kwargs: keyword argmument dict for visualization

        Raises:
            ValueError: If the wrong number of faces labels are provided

        Returns:
            gpd.GeoDataFrame: Merged data
        """
        # Compute the working projected CRS
        # This is important because having things in meters makes things easier
        self.logger.info("Computing working CRS")
        verts_in_lon_lat = self.get_vertices_in_CRS(output_CRS=LAT_LON_EPSG_CODE)
        lon, lat, _ = verts_in_lon_lat[0]
        working_CRS = get_projected_CRS(lon=lon, lat=lat)

        if face_labels is None:
            face_labels = self.get_texture(request_vertex_texture=False)

        # Check that the correct number of labels are provided
        if len(face_labels) != self.faces.shape[0]:
            raise ValueError()

        face_labels = np.squeeze(face_labels)

        self.logger.info("Computing faces in working CRS")
        # Get the mesh vertices in the desired export CRS
        verts_in_crs = self.get_vertices_in_CRS(working_CRS)
        # Get a triangle in geospatial coords for each face
        # (n_faces, 3 points, xyz)
        faces = verts_in_crs[self.faces]
        # Drop the z axis
        faces_2d = faces[..., :2]

        # Extract the faces for each unique label

        face_tuples = [tuple(map(tuple, a)) for a in faces_2d]
        face_polygons = [
            Polygon(face_tuple)
            for face_tuple in tqdm(face_tuples, desc=f"Converting faces to polygons")
        ]
        self.logger.info("Creating dataframe of faces")
        # Convert these faces to multipolygons

        faces_gdf = gpd.GeoDataFrame(
            {CLASS_ID_KEY: face_labels.tolist()},
            geometry=face_polygons,
            crs=working_CRS,
        )
        self.logger.info("Creating dataframe of multipolygons")
        unique_IDs = np.unique(face_labels)
        if drop_nan:
            # Drop nan from the list of IDs
            unique_IDs = unique_IDs[np.isfinite(unique_IDs)]
        multipolygon_list = []
        # For each unique ID, aggregate all the faces together
        # This is the same as geopandas.groupby, but that is slow and can out of memory easily
        # due to the large number of polygons
        # Instead, we replace the default shapely.unary_union with our batched implementation
        for unique_ID in unique_IDs:
            matching_face_polygons = faces_gdf.iloc[face_labels == unique_ID]
            list_of_polygons = matching_face_polygons.geometry.values
            multipolygon = batched_unary_union(
                list_of_polygons, **batched_unary_union_kwargs
            )
            multipolygon_list.append(multipolygon)

        working_gdf = gpd.GeoDataFrame(
            {CLASS_ID_KEY: unique_IDs}, geometry=multipolygon_list, crs=working_CRS
        )

        if label_names is not None:
            names = [
                (label_names[int(ID)] if np.isfinite(ID) else "nan")
                for ID in working_gdf[CLASS_ID_KEY]
            ]
            working_gdf[CLASS_NAMES_KEY] = names

        # Simplify the output geometry
        if simplify_tol > 0.0:
            self.logger.info("Running simplification")
            working_gdf.geometry = working_gdf.geometry.simplify(simplify_tol)

        # Make sure that the polygons are non-overlapping
        if ensure_non_overlapping:
            # TODO create a version that tie-breaks based on the number of predicted faces for each
            # class and optionally the ratios of 3D to top-down areas for the input triangles.
            self.logger.info("Ensuring non-overlapping polygons")
            working_gdf = ensure_non_overlapping_polygons(working_gdf)

        # Transform from the working crs to export crs
        export_gdf = working_gdf.to_crs(export_crs)

        # Vis if requested
        if vis:
            self.logger.info("Plotting")
            export_gdf.plot(
                column=CLASS_NAMES_KEY if label_names is not None else CLASS_ID_KEY,
                aspect=1,
                legend=True,
                **vis_kwargs,
            )
            plt.show()

        # Export if a file is provided
        if export_file is not None:
            export_gdf.to_file(export_file)

        return export_gdf

    # Operations on raster files

    def get_vert_values_from_raster_file(
        self,
        raster_file: PATH_TYPE,
        return_verts_in_CRS: bool = False,
        nodata_fill_value: float = np.nan,
    ):
        """Compute the height above groun for each point on the mesh

        Args:
            raster_file (PATH_TYPE, optional): The path to the geospatial raster file.
            return_verts_in_CRS (bool, optional): Return the vertices transformed into the raster CRS
            nodata_fill_value (float, optional): Set data defined by the opened file as NODATAVAL to this value

        Returns:
            np.ndarray: samples from raster. Either (n_verts,) or (n_verts, n_raster_channels)
            np.ndarray (optional): (n_verts, 3) the vertices in the raster CRS
        """
        # Open the DTM file
        raster = rio.open(raster_file)
        # Get the mesh points in the coordinate reference system of the DTM
        verts_in_raster_CRS = self.get_vertices_in_CRS(
            raster.crs, force_easting_northing=True
        )

        # Get the points as a list
        easting_points = verts_in_raster_CRS[:, 0].tolist()
        northing_points = verts_in_raster_CRS[:, 1].tolist()

        # Zip them together
        zipped_locations = zip(easting_points, northing_points)
        sampling_iter = tqdm(
            zipped_locations,
            desc=f"Sampling values from raster {raster_file}",
            total=verts_in_raster_CRS.shape[0],
        )
        # Sample the raster file and squeeze if single channel
        sampled_raster_values = np.squeeze(np.array(list(raster.sample(sampling_iter))))

        # Set nodata locations to nan
        # TODO figure out if it will ever be a problem to take the first value
        sampled_raster_values[sampled_raster_values == raster.nodatavals[0]] = (
            nodata_fill_value
        )

        if return_verts_in_CRS:
            return sampled_raster_values, verts_in_raster_CRS

        return sampled_raster_values

    def get_height_above_ground(
        self, DTM_file: PATH_TYPE, threshold: float = None
    ) -> np.ndarray:
        """Return height above ground for a points in the mesh and a given DTM

        Args:
            DTM_file (PATH_TYPE): Path to the digital terrain model raster
            threshold (float, optional):
                If not None, return a boolean mask for points under this height. Defaults to None.

        Returns:
            np.ndarray: Either the height above ground or a boolean mask for ground points
        """
        # Get the height from the DTM and the points in the same CRS
        DTM_heights, verts_in_raster_CRS = self.get_vert_values_from_raster_file(
            DTM_file, return_verts_in_CRS=True
        )
        # Extract the vertex height as the third channel
        verts_height = verts_in_raster_CRS[:, 2]
        # Subtract the two to get the height above ground
        height_above_ground = verts_height - DTM_heights

        # If the threshold is not None, return a boolean mask that is true for ground points
        if threshold is not None:
            # Return boolean mask
            # TODO see if this will break for nan values
            return height_above_ground < threshold
        # Return height above ground
        return height_above_ground

    def label_ground_class(
        self,
        DTM_file: PATH_TYPE,
        height_above_ground_threshold: float,
        labels: typing.Union[None, np.ndarray] = None,
        only_label_existing_labels: bool = True,
        ground_class_name: str = "ground",
        ground_ID: typing.Union[None, int] = None,
        set_mesh_texture: bool = False,
    ) -> np.ndarray:
        """
        Set vertices to a potentially-new class with a thresholded height above the DTM.
        TODO, consider handling face textures as well

        Args:
            DTM_file (PATH_TYPE): Path to the DTM file
            height_above_ground_threshold (float): Height (meters) above that DTM that points below are considered ground
            labels (typing.Union[None, np.ndarray], optional): Vertex texture, otherwise will be queried from mesh. Defaults to None.
            only_label_existing_labels (bool, optional): Only label points that already have non-null labels. Defaults to True.
            ground_class_name (str, optional): The potentially-new ground class name. Defaults to "ground".
            ground_ID (typing.Union[None, int], optional): What value to use for the ground class. Will be set inteligently if not provided. Defaults to None.

        Returns:
            np.ndarray: The updated labels
        """

        if labels is None:
            # Default to using vertex labels since it's the native way to check height above the DTM
            use_vertex_labels = True
        elif labels is not None:
            # Check the size of the input labels and set what type they are. Note this could override existing value
            if labels.shape[0] == self.pyvista_mesh.points.shape[0]:
                use_vertex_labels = True
            elif labels.shape[0] == self.faces.shape[0]:
                use_vertex_labels = False
            else:
                raise ValueError(
                    "Labels were provided but didn't match the shape of vertices or faces"
                )

        # if a labels are not provided, get it from the mesh
        if labels is None:
            # Get the vertex textures from the mesh
            labels = self.get_texture(
                request_vertex_texture=use_vertex_labels,
            )

        # Compute which vertices are part of the ground by thresholding the height above the DTM
        ground_mask = self.get_height_above_ground(
            DTM_file=DTM_file, threshold=height_above_ground_threshold
        )
        # If we needed a mask for the faces, compute that instead
        if not use_vertex_labels:
            ground_mask = self.vert_to_face_texture(ground_mask.astype(int)).astype(
                bool
            )

        # Replace only vertices that were previously labeled as something else, to avoid class imbalance
        if only_label_existing_labels:
            # Find which vertices are labeled
            is_labeled = np.isfinite(labels[:, 0])
            # Find which points are ground that were previously labeled as something else
            ground_mask = np.logical_and(is_labeled, ground_mask)

        # Get the existing label names
        IDs_to_labels = self.get_IDs_to_labels()

        if IDs_to_labels is None and ground_ID is None:
            # This means that the label is continous, so the concept of ID is meaningless
            ground_ID = np.nan
        elif IDs_to_labels is not None and ground_class_name in IDs_to_labels:
            # If the ground class name is already in the list, set newly-predicted vertices to that class
            ground_ID = IDs_to_labels.find(ground_class_name)
        elif IDs_to_labels is not None:
            # If the label names are present, and the class is not already included, add it as the last element
            if ground_ID is None:
                # Set it to the first unused ID
                # TODO improve this since it should be the max plus one
                ground_ID = len(IDs_to_labels)

        self.add_label(label_name=ground_class_name, label_ID=ground_ID)

        # Replace mask for ground_vertices
        labels[ground_mask, 0] = ground_ID

        # Optionally apply the texture to the mesh
        if set_mesh_texture:
            self.set_texture(labels, use_derived_IDs_to_labels=False)

        return labels

    # Expensive pixel-to-vertex operations

    def get_rasterization_results_pytorch3d(
        self,
        cameras: typing.Union[PhotogrammetryCameraSet, PhotogrammetryCamera],
        image_scale: float = 1.0,
        cull_to_frustum: bool = False,
    ):
        """Use pytorch3d to get correspondences between pixels and vertices

        Args:
            camera (PhotogrammetryCamera): Camera to get raster for
            img_scale (float): How much to resize the image by

        Returns:
            pytorch3d.PerspectiveCamera: The camera corresponding to the index
            pytorch3d.Fragments: The rendering results from the rasterer, before the shader
        """
        # Promote to one-length list if only one camera is passed

        # Create a camera from the metashape parameters
        p3d_cameras = cameras.get_pytorch3d_camera(device=self.device)
        if isinstance(cameras, PhotogrammetryCamera):
            image_size = cameras.get_image_size(image_scale=image_scale)
        else:
            image_size = cameras.get_camera_by_index(0).get_image_size(
                image_scale=image_scale
            )

        raster_settings = RasterizationSettings(
            image_size=image_size,
            blur_radius=0.0,
            faces_per_pixel=1,
            cull_to_frustum=cull_to_frustum,
        )

        # Don't wrap this in a MeshRenderer like normal because we need intermediate results
        rasterizer = MeshRasterizer(
            cameras=p3d_cameras, raster_settings=raster_settings
        ).to(self.device)

        # Ensure that a pytorch3d mesh exists
        self.create_pytorch3d_mesh(batch_size=len(p3d_cameras))
        # Perform the expensive pytorch3d operation
        fragments = rasterizer(self.pytorch3d_mesh)
        return p3d_cameras, fragments

    def aggregate_viewpoints_pytorch3d(
        self,
        camera_set: PhotogrammetryCameraSet,
        image_scale: float = 1.0,
        batch_size: int = 1,
    ) -> typing.Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Aggregate information from different viepoints onto the mesh faces using pytorch3d.
        This considers occlusions but is fairly slow

        Args:
            camera_set (PhotogrammetryCamera): Set of cameras to aggregate
            image_scale (float): Scale images
        """
        if batch_size != 1:
            raise NotImplementedError(
                "Proper batching is not implemented, set batch size to 1"
            )
        # This is where the colors will be aggregated
        # This should be big enough to not overflow
        n_channels = camera_set.n_image_channels()
        face_texture = np.zeros(
            (self.pyvista_mesh.n_faces, n_channels), dtype=np.uint32
        )
        counts = np.zeros(self.pyvista_mesh.n_faces, dtype=np.uint16)

        # Go through all cameras in a random order
        camera_inds = np.arange(len(camera_set.cameras))
        np.random.shuffle(camera_inds)

        for batch_start in tqdm(
            range(0, len(camera_inds), batch_size),
            desc="Aggregating information from different viewpoints",
        ):
            # Get the photogrammetry cameras for the batch
            batch_cameras = camera_set.get_subset_cameras(
                camera_inds[batch_start : batch_start + batch_size]
            )
            # Do the expensive step to get pixel-to-vertex correspondences
            _, fragments = self.get_rasterization_results_pytorch3d(
                cameras=batch_cameras, image_scale=image_scale
            )
            # Do the update step independently for each of the images
            for i in range(batch_size):
                # Load the image
                img = batch_cameras.get_image_by_index(i, image_scale=image_scale)
                img_shape = img.shape

                # Set up indices for indexing into the image
                inds = np.meshgrid(
                    np.arange(img_shape[0]), np.arange(img_shape[1]), indexing="ij"
                )
                flat_i_inds = inds[0].flatten()
                flat_j_inds = inds[1].flatten()

                ## Aggregate image information using the correspondences
                # Extract the correspondences as a flat array
                pix_to_face = fragments.pix_to_face[i, :, :, 0].cpu().numpy().flatten()
                # Build an array to store the new colors
                new_texture = np.zeros(
                    (self.pyvista_mesh.n_faces, n_channels), dtype=np.uint32
                )
                # Index the image to fill this array
                # TODO find a way to do this better if there are multiple pixels per face
                # now that behaviour is undefined, I assume the last on indexed just overrides the previous ones
                new_texture[pix_to_face] = img[flat_i_inds, flat_j_inds]
                # Update the face colors
                face_texture = face_texture + new_texture
                # Find unique face indices because we can't increment multiple times like ths
                unique_faces = np.unique(pix_to_face)
                # TODO Consider ditching counts array since we can sum over all values in the face texture
                counts[unique_faces] = counts[unique_faces] + 1

        normalized_face_texture = face_texture / np.expand_dims(counts, 1)
        return normalized_face_texture, face_texture, counts

    def aggregate_viewpoints_pytorch3d_by_cluster(
        self,
        camera_set: PhotogrammetryCameraSet,
        image_scale: float = 1.0,
        batch_size: int = 1,
        n_clusters: int = 8,
        buffer_dist_meters=50,
        vis_clusters: bool = False,
    ) -> typing.Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Aggregate viewpoints efficiently by chunking the mesh into clusters

        Args:
            camera_set (PhotogrammetryCameraSet): Same as aggregate_viewpoints_pytorch3d
            image_scale (float, optional): Same as aggregate_viewpoints_pytorch3d. Defaults to 1.0.
            batch_size (int, optional): Same as aggregate_viewpoints_pytorch3d. Defaults to 1.
            n_clusters (int, optional): Number of clusters to partition the cameras into. Defaults to 8.
            buffer_dist_meters (int, optional): Distance from cameras to include in mesh subset. Defaults to 50.
            vis_clusters (bool, optional): Show the camera clusters. Defaults to False.

        Returns:
            Same as aggregate_viewpoints_pytorch3d
        """
        # Get the lat lon for each camera point and turn into a shapely Point
        camera_points = [Point(*cp) for cp in camera_set.get_lon_lat_coords()]
        # Create a geodataframe from the points
        camera_points = gpd.GeoDataFrame(
            geometry=camera_points, crs=pyproj.CRS.from_epsg("4326")
        )
        # Make sure the gdf has a gemetric CRS so there is no warping of the space
        camera_points = ensure_geometric_CRS(camera_points)
        # Extract the x, y points now in a geometric CRS
        camera_points_numpy = np.stack(
            camera_points.geometry.apply(lambda point: (point.x, point.y))
        )

        # Assign each camera to a cluster
        camera_cluster_IDs = KMeans(n_clusters=n_clusters).fit_predict(
            camera_points_numpy
        )
        if vis_clusters:
            plt.scatter(
                camera_points_numpy[:, 0],
                camera_points_numpy[:, 1],
                c=camera_cluster_IDs,
                cmap="tab20",
            )
            plt.show()

        # Build the bookkeeping objects
        n_points = self.faces.shape[0]
        all_textures = np.zeros(
            (n_points, camera_set.n_image_channels()), dtype=np.uint16
        )
        all_counts = np.zeros(n_points, dtype=np.uint16)

        for cluster_ID in tqdm(range(n_clusters), desc="Chunks in mesh"):
            # Get indices of cameras for that cluster
            matching_camera_inds = np.where(cluster_ID == camera_cluster_IDs)[0]
            # Extract the rows in the dataframe for those IDs
            subset_camera_points = camera_points.iloc[matching_camera_inds]

            # TODO this could be accellerated by computing the membership for all points at the begining.
            # This would require computing all the ROIs (potentially-overlapping) for each region first. Then, finding all the non-overlapping
            # partition where each polygon corresponds to a set of ROIs. Then the membership for each vertex could be found for each polygon
            # and the membership in each ROI could be computed. This should be benchmarked though, because having more polygons than original
            # ROIs may actually lead to slower computations than doing it sequentially

            # Extract a sub mesh for a region around the camera points and also retain the indices into the original mesh
            sub_mesh_pv, _, face_IDs = self.select_mesh_ROI(
                region_of_interest=subset_camera_points,
                buffer_meters=buffer_dist_meters,
                return_original_IDs=True,
            )
            # Wrap this pyvista mesh in a photogrammetry mesh
            sub_mesh_TPM = TexturedPhotogrammetryMesh(sub_mesh_pv)

            # Get the segmentor camera set for the subset of the camera inds
            sub_camera_set = camera_set.get_subset_cameras(matching_camera_inds)
            # Perform aggregation with the subset of camera and the subset of the mesh
            _, face_texture, counts = sub_mesh_TPM.aggregate_viewpoints_pytorch3d(
                sub_camera_set, image_scale=image_scale, batch_size=batch_size
            )
            # Index back into the full array for bookkeeping
            all_textures[face_IDs] = all_textures[face_IDs] + face_texture
            all_counts[face_IDs] = all_counts[face_IDs] + counts

        normalized_face_texture = all_textures / np.expand_dims(all_counts, 1)
        return normalized_face_texture, all_textures, all_counts

    def aggregate_viewpoints_naive(self, camera_set: PhotogrammetryCameraSet):
        """
        Aggregate the information from all images onto the mesh without considering occlusion
        or distortion parameters

        Args:
            camera_set (PhotogrammetryCameraSet): Camera set to use for aggregation
        """
        # Initialize a masked array to record values
        summed_values = np.zeros((self.pyvista_mesh.points.shape[0], 3))

        counts = np.zeros((self.pyvista_mesh.points.shape[0], 3))
        for i in tqdm(range(len(camera_set.cameras))):
            # This is actually the bottleneck in the whole process
            img = camera_set.get_camera_by_index(i).load_image()
            colors_per_vertex = camera_set.cameras[i].project_mesh_verts(
                self.pyvista_mesh.points, img, device=self.device
            )
            summed_values = summed_values + colors_per_vertex.data
            counts[np.logical_not(colors_per_vertex.mask)] = (
                counts[np.logical_not(colors_per_vertex.mask)] + 1
            )
        mean_colors = (summed_values / counts).astype(np.uint8)
        plotter = pv.Plotter()
        plotter.add_mesh(self.pyvista_mesh, scalars=mean_colors, rgb=True)
        plotter.show()

    def render_pyvista(
        self,
        camera_set,
        camera_index: int,
        image_scale: float = 1.0,
        enable_ssao: bool = False,
    ):
        """Render an image from the viewpoint of a single camera. Note that the principle point is ignored.

        Args:
            camera_set (PhotogrammetryCameraSet): Camera set to use for rendering
            camera_index (int): which camera to render
            image_scale (float, optional):
                Multiplier on the real image scale to obtain size for rendering. Lower values
                yield a lower-resolution render but the runtime is quiker. Defaults to 1.0.
        """
        # Get the pyvista camera and image size
        pv_camera = camera_set.get_camera_by_index(camera_index).get_pyvista_camera()
        image_size = camera_set.get_camera_by_index(camera_index).get_image_size()

        # Transform image size appropriately
        scaled_image_size_xy = (
            int(round(image_size[1] * image_scale)),
            int(round(image_size[0] * image_scale)),
        )

        # Get the texture from the mesh
        texture = self.get_texture(request_vertex_texture=False)
        is_RGB = texture.shape[1] == 3

        # Set up the pyvista plotter
        plotter = pv.Plotter(off_screen=True)
        # Add the mesh
        plotter.add_mesh(
            self.pyvista_mesh,
            scalars=texture,
            rgb=is_RGB,
        )
        # Set the camera to the requested viewpoint
        plotter.camera = pv_camera

        if enable_ssao:
            plotter.enable_ssao()
            # plotter.renderer.enable_ssao(radius=3)
        # Perform the rendering operation
        rendered_img = plotter.screenshot(
            window_size=scaled_image_size_xy,
        )
        return rendered_img

    def render_pytorch3d(
        self,
        camera_set: PhotogrammetryCameraSet,
        camera_index: int,
        image_scale: float = 1.0,
        shade_by_indexing: bool = None,
        set_null_texture_to_value: float = None,
    ):
        """Render an image from the viewpoint of a single camera

        Args:
            camera_set (PhotogrammetryCameraSet): Camera set to use for rendering
            camera_index (int): which camera to render
            image_scale (float, optional):
                Multiplier on the real image scale to obtain size for rendering. Lower values
                yield a lower-resolution render but the runtime is quiker. Defaults to 1.0.
            shade_by_indexing (bool, optional): Use indexing rather than a pytorch3d shader. Useful for integer labels
        """
        if shade_by_indexing is None:
            shade_by_indexing = self.is_discrete_texture()

        # TODO clean up
        texture = self.get_texture(request_vertex_texture=(not shade_by_indexing))
        # Get the texture and set it
        self.create_pytorch3d_mesh(vert_texture=None if shade_by_indexing else texture)

        # Get the photogrametery camera
        pg_camera = camera_set.get_camera_by_index(camera_index)

        # Compute the pixel-to-vertex correspondences, this is expensive
        p3d_camera, fragments = self.get_rasterization_results_pytorch3d(
            pg_camera, image_scale=image_scale
        )

        # Use the indexing method
        if shade_by_indexing:
            # Extract the pixel to face correspondences
            pix_to_face = fragments.pix_to_face[0, :, :, 0].cpu().numpy().flatten()
            # Index into the texture image
            flat_labels = texture[pix_to_face]
            # Remap the value pixels that don't correspond to a face, which are labeled -1
            if set_null_texture_to_value is not None:
                flat_labels[pix_to_face == -1] = set_null_texture_to_value

            # Reshape from flat to an array
            img_size = pg_camera.get_image_size(image_scale=image_scale)
            texture_channels = () if texture.shape[1] == 1 else (texture.shape[1],)
            label_img = np.reshape(flat_labels, img_size + texture_channels)
        else:
            # Create ambient light so it doesn't effect the color
            lights = AmbientLights(device=self.device)
            # Create a shader
            shader = HardGouraudShader(
                device=self.device, cameras=p3d_camera, lights=lights
            )

            # Render the images using the shader
            label_img = shader(fragments, self.pytorch3d_mesh)[0].cpu().numpy()

        return label_img

    # Visualization and saving methods
    def vis(
        self,
        interactive=True,
        camera_set: PhotogrammetryCameraSet = None,
        screenshot_filename: PATH_TYPE = None,
        vis_scalars=None,
        mesh_kwargs: typing.Dict = None,
        plotter_kwargs: typing.Dict = {},
        enable_ssao: bool = True,
        force_xvfb: bool = False,
    ):
        """Show the mesh and cameras

        Args:
            off_screen (bool, optional): Show offscreen
            camera_set (PhotogrammetryCameraSet, optional): Cameras to visualize. Defaults to None.
            screenshot_filename (PATH_TYPE, optional): Filepath to save to, will show interactively if None. Defaults to None.
            vis_scalars: Scalars to show
            mesh_kwargs: dict of keyword arguments for the mesh
            plotter_kwargs: dict of keyword arguments for the plotter
        """
        off_screen = (not interactive) or (screenshot_filename is not None)
        if off_screen or force_xvfb:
            pv.start_xvfb()

        # Set the mesh kwargs if not set
        if mesh_kwargs is None:
            if self.is_discrete_texture() and len(self.get_label_names()) <= 10:
                mesh_kwargs = TEN_CLASS_VIS_KWARGS
            elif self.is_discrete_texture() and len(self.get_label_names()) <= 20:
                mesh_kwargs = TWENTY_CLASS_VIS_KWARGS
            else:
                # More than 20 class or continous values
                mesh_kwargs = {}

        # Create the plotter which may be onscreen or off
        plotter = pv.Plotter(off_screen=off_screen)

        # If the vis scalars are None, use the saved texture
        if vis_scalars is None:
            vis_scalars = self.get_texture(
                # Request vertex texture if both are available
                request_vertex_texture=(
                    True
                    if (
                        self.vertex_texture is not None
                        and self.face_texture is not None
                    )
                    else None
                )
            )

        is_rgb = (
            self.pyvista_mesh.active_scalars_name == "RGB"
            if vis_scalars is None
            else (vis_scalars.ndim == 2 and vis_scalars.shape[1] > 1)
        )

        # Data in the range [0, 255] must
        if is_rgb and np.max(vis_scalars) > 1.0:
            vis_scalars = np.clip(vis_scalars, 0, 255).astype(np.uint8)

        scalar_bar_args = {"vertical": True}
        if self.is_discrete_texture() and "annotations" not in mesh_kwargs:
            mesh_kwargs["annotations"] = self.IDs_to_labels
            scalar_bar_args["n_labels"] = 0

        # Add the mesh
        plotter.add_mesh(
            self.pyvista_mesh,
            scalars=vis_scalars,
            rgb=is_rgb,
            scalar_bar_args=scalar_bar_args,
            **mesh_kwargs,
        )
        # If the camera set is provided, show this too
        if camera_set is not None:
            camera_set.vis(plotter, add_orientation_cube=False)

        # Enable screen space shading
        if enable_ssao:
            plotter.enable_ssao()

        # Create parent folder if none exists
        if screenshot_filename is not None:
            Path(screenshot_filename).parent.mkdir(parents=True, exist_ok=True)

        # Show
        return plotter.show(screenshot=screenshot_filename, **plotter_kwargs)

    def save_renders_pytorch3d(
        self,
        camera_set: PhotogrammetryCameraSet,
        render_image_scale=1.0,
        camera_indices=None,
        output_folder: PATH_TYPE = Path(VIS_FOLDER, "renders"),
        make_composites: bool = False,
        blend_composite: bool = True,
        save_native_resolution: bool = False,
        set_null_texture_to_value: float = 255,
    ):
        """Render an image from the viewpoint of each specified camera and save a composite

        Args:
            camera_set (PhotogrammetryCameraSet): Camera set to use for rendering
            render_image_scale (float, optional):
                Multiplier on the real image scale to obtain size for rendering. Lower values
                yield a lower-resolution render but the runtime is quiker. Defaults to 1.0.
            camera_indices (ArrayLike | NoneType, optional): Indices to render. If None, render all in a random order
            render_folder (PATH_TYPE, optional): Save images to this folder within vis. Default "renders"
            make_composites (bool, optional): Should a triple composite the original image be saved
            blend_composite (bool, optional): Should the real and rendered image be saved, rather than inserting a channel of the render into the real
        """
        # Render each image individually.
        # TODO this could be accelerated by inteligent batching
        if camera_indices is None:
            camera_indices = np.arange(camera_set.n_cameras())
            np.random.shuffle(camera_indices)

        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Saving renders to {output_folder}")

        # Save the classes filename
        if self.is_discrete_texture():
            IDs_to_labels_file = Path(output_folder, "IDs_to_labels.json")
            self.logger.info(f"Saving IDs_to_labels to {str(IDs_to_labels_file)}")
            with open(IDs_to_labels_file, "w") as outfile_h:
                json.dump(self.IDs_to_labels, outfile_h, ensure_ascii=False, indent=4)

        for i in tqdm(camera_indices, desc="Saving renders"):
            rendered = self.render_pytorch3d(
                camera_set=camera_set,
                camera_index=i,
                image_scale=render_image_scale,
                set_null_texture_to_value=set_null_texture_to_value,
            )

            # Clip channels if needed
            if rendered.ndim == 3:
                rendered = rendered[..., :3]

            if save_native_resolution:
                native_size = camera_set.get_camera_by_index(i).get_image_size()
                # Upsample using nearest neighbor interpolation for discrete labels and
                # bilinear for non-discrete
                rendered = resize(
                    rendered,
                    native_size,
                    order=(0 if self.is_discrete_texture() else 1),
                )

            if make_composites:
                real_img = camera_set.get_camera_by_index(i).get_image(
                    image_scale=(1.0 if save_native_resolution else render_image_scale)
                )[..., :3]

                if blend_composite:
                    combined = (real_img + rendered) / 2.0
                else:
                    combined = real_img.copy().astype(float)
                    combined[..., 0] = rendered[..., 0]
                rendered = np.clip(
                    np.concatenate((real_img, rendered, combined), axis=1),
                    0,
                    1,
                )

            # rendered = (rendered * 255).astype(np.uint8)
            rendered = rendered.astype(np.uint8)
            output_filename = Path(
                output_folder, camera_set.get_image_filename(i, absolute=False)
            )
            # This may create nested folders in the output dir
            output_filename.parent.mkdir(parents=True, exist_ok=True)
            output_filename = str(output_filename.with_suffix(".png"))
            # Save the image
            skimage.io.imsave(output_filename, rendered, check_contrast=False)
