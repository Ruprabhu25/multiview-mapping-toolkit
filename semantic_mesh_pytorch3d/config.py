from pathlib import Path

DATA_FOLDER = Path(Path(__file__).parent, "..", "data").resolve()

DEFAULT_IMAGES_FOLDER = str(Path(DATA_FOLDER, "composite_20230520T0519", "images"))

DEFAULT_LOCAL_MESH = str(
    Path(
        DATA_FOLDER,
        "composite_georef",
        "composite_local_reset.ply",
    )
)

DEFAULT_GEOREF_MESH = str(
    Path(
        DATA_FOLDER,
        "composite_georef",
        "composite_georef.ply",
    )
)
DEFAULT_GEOREF_MESH = "/ofo-share/repos-david/semantic-mesh-pytorch3d/data/2023-08-23_1027_QR_F1_rgb_100m_25_img/exports/2023-08-23_1027_QR_F1_rgb_100m_10_img_20230926T1520_model_georeferenced_manual_export.ply"

DEFAULT_CAM_FILE = str(
    Path(
        DATA_FOLDER,
        "composite_georef",
        "composite_georef_cameras.xml",
    )
)

DEFAULT_GEOFILE = str(
    Path(DATA_FOLDER, "composite_20230520T0519", "composite_20230520T0519_crowns.gpkg")
)
