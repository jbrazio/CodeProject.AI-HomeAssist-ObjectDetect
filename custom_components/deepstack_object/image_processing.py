"""
Component that will perform object detection and identification via deepstack.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/image_processing.deepstack_object
"""
from collections import namedtuple
import datetime
import io
import logging
import os
import re
from datetime import timedelta
from typing import Tuple, Dict, List
from pathlib import Path

from PIL import Image, ImageDraw

import deepstack.core as ds
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.util.pil import draw_box
from homeassistant.components.image_processing import (
    ATTR_CONFIDENCE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    DEFAULT_CONFIDENCE,
    DOMAIN,
    PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_IP_ADDRESS,
    CONF_PORT,
    HTTP_BAD_REQUEST,
    HTTP_OK,
    HTTP_UNAUTHORIZED,
)
from homeassistant.core import split_entity_id

_LOGGER = logging.getLogger(__name__)

ANIMAL = "animal"
ANIMALS = [
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
]
OTHER = "other"
PERSON = "person"
VEHICLE = "vehicle"
VEHICLES = ["bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck"]

CONF_API_KEY = "api_key"
CONF_TARGET = "target"
CONF_TARGETS = "targets"
CONF_TIMEOUT = "timeout"
CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"
CONF_SHOW_BOXES = "show_boxes"
CONF_ROI_Y_MIN = "roi_y_min"
CONF_ROI_X_MIN = "roi_x_min"
CONF_ROI_Y_MAX = "roi_y_max"
CONF_ROI_X_MAX = "roi_x_max"
CONF_CUSTOM_MODEL = "custom_model"

DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_API_KEY = ""
DEFAULT_TARGETS = [{CONF_TARGET: PERSON, ATTR_CONFIDENCE: DEFAULT_CONFIDENCE}]
DEFAULT_TIMEOUT = 10
DEFAULT_ROI_Y_MIN = 0.0
DEFAULT_ROI_Y_MAX = 1.0
DEFAULT_ROI_X_MIN = 0.0
DEFAULT_ROI_X_MAX = 1.0
DEFAULT_ROI = (
    DEFAULT_ROI_Y_MIN,
    DEFAULT_ROI_X_MIN,
    DEFAULT_ROI_Y_MAX,
    DEFAULT_ROI_X_MAX,
)

EVENT_OBJECT_DETECTED = "deepstack.object_detected"
BOX = "box"
FILE = "file"
OBJECT = "object"
SAVED_FILE = "saved_file"

# rgb(red, green, blue)
RED = (255, 0, 0)  # For objects within the ROI
GREEN = (0, 255, 0)  # For ROI box
YELLOW = (255, 255, 0)  # Unused

TARGETS_SCHEMA = {
    vol.Required(CONF_TARGET): cv.string,
    vol.Optional(ATTR_CONFIDENCE, default=DEFAULT_CONFIDENCE): vol.All(
        vol.Coerce(float), vol.Range(min=0, max=100)
    ),
}


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_IP_ADDRESS): cv.string,
        vol.Required(CONF_PORT): cv.port,
        vol.Optional(CONF_API_KEY, default=DEFAULT_API_KEY): cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        vol.Optional(CONF_CUSTOM_MODEL, default=""): cv.string,
        vol.Optional(CONF_TARGETS, default=DEFAULT_TARGETS): vol.All(
            cv.ensure_list, [vol.Schema(TARGETS_SCHEMA)]
        ),
        vol.Optional(CONF_ROI_Y_MIN, default=DEFAULT_ROI_Y_MIN): cv.small_float,
        vol.Optional(CONF_ROI_X_MIN, default=DEFAULT_ROI_X_MIN): cv.small_float,
        vol.Optional(CONF_ROI_Y_MAX, default=DEFAULT_ROI_Y_MAX): cv.small_float,
        vol.Optional(CONF_ROI_X_MAX, default=DEFAULT_ROI_X_MAX): cv.small_float,
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
        vol.Optional(CONF_SHOW_BOXES, default=True): cv.boolean,
    }
)

Box = namedtuple("Box", "y_min x_min y_max x_max")
Point = namedtuple("Point", "y x")


def point_in_box(box: Box, point: Point) -> bool:
    """Return true if point lies in box"""
    if (box.x_min <= point.x <= box.x_max) and (box.y_min <= point.y <= box.y_max):
        return True
    return False


def object_in_roi(roi: dict, centroid: dict) -> bool:
    """Convenience to convert dicts to the Point and Box."""
    target_center_point = Point(centroid["y"], centroid["x"])
    roi_box = Box(roi["y_min"], roi["x_min"], roi["y_max"], roi["x_max"])
    return point_in_box(roi_box, target_center_point)


def get_valid_filename(name: str) -> str:
    return re.sub(r"(?u)[^-\w.]", "", str(name).strip().replace(" ", "_"))


def get_object_type(object_name: str) -> str:
    if object_name == PERSON:
        return PERSON
    elif object_name in ANIMALS:
        return ANIMAL
    elif object_name in VEHICLES:
        return VEHICLE
    else:
        return OTHER


def get_objects(predictions: list, img_width: int, img_height: int) -> List[Dict]:
    """Return objects with formatting and extra info."""
    objects = []
    decimal_places = 3
    for pred in predictions:
        box_width = pred["x_max"] - pred["x_min"]
        box_height = pred["y_max"] - pred["y_min"]
        box = {
            "height": round(box_height / img_height, decimal_places),
            "width": round(box_width / img_width, decimal_places),
            "y_min": round(pred["y_min"] / img_height, decimal_places),
            "x_min": round(pred["x_min"] / img_width, decimal_places),
            "y_max": round(pred["y_max"] / img_height, decimal_places),
            "x_max": round(pred["x_max"] / img_width, decimal_places),
        }
        box_area = round(box["height"] * box["width"], decimal_places)
        centroid = {
            "x": round(box["x_min"] + (box["width"] / 2), decimal_places),
            "y": round(box["y_min"] + (box["height"] / 2), decimal_places),
        }
        name = pred["label"]
        object_type = get_object_type(name)
        confidence = round(pred["confidence"] * 100, decimal_places)

        objects.append(
            {
                "bounding_box": box,
                "box_area": box_area,
                "centroid": centroid,
                "name": name,
                "object_type": object_type,
                "confidence": confidence,
            }
        )
    return objects


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the classifier."""
    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    targets = config[CONF_TARGETS]  # ensure lower case
    entities = []
    for camera in config[CONF_SOURCE]:
        object_entity = ObjectClassifyEntity(
            ip_address=config.get(CONF_IP_ADDRESS),
            port=config.get(CONF_PORT),
            api_key=config.get(CONF_API_KEY),
            timeout=config.get(CONF_TIMEOUT),
            custom_model=config.get(CONF_CUSTOM_MODEL),
            targets=targets,
            confidence=config.get(ATTR_CONFIDENCE),
            roi_y_min=config[CONF_ROI_Y_MIN],
            roi_x_min=config[CONF_ROI_X_MIN],
            roi_y_max=config[CONF_ROI_Y_MAX],
            roi_x_max=config[CONF_ROI_X_MAX],
            show_boxes=config[CONF_SHOW_BOXES],
            save_file_folder=save_file_folder,
            save_timestamped_file=config.get(CONF_SAVE_TIMESTAMPTED_FILE),
            camera_entity=camera.get(CONF_ENTITY_ID),
            name=camera.get(CONF_NAME),
        )
        entities.append(object_entity)
    add_devices(entities)


class ObjectClassifyEntity(ImageProcessingEntity):
    """Perform a face classification."""

    def __init__(
        self,
        ip_address,
        port,
        api_key,
        timeout,
        custom_model,
        targets,
        confidence,
        roi_y_min,
        roi_x_min,
        roi_y_max,
        roi_x_max,
        show_boxes,
        save_file_folder,
        save_timestamped_file,
        camera_entity,
        name=None,
    ):
        """Init with the API key and model id."""
        super().__init__()
        self._dsobject = ds.DeepstackObject(
            ip=ip_address,
            port=port,
            api_key=api_key,
            timeout=timeout,
            min_confidence=confidence / 100,
            custom_model=custom_model,
        )
        self._custom_model = custom_model
        self._targets = targets
        self._confidence = confidence
        self._camera = camera_entity
        if name:
            self._name = name
        else:
            camera_name = split_entity_id(camera_entity)[1]
            self._name = "deepstack_object_{}".format(camera_name)

        self._state = None
        self._objects = []  # The parsed raw data
        self._targets_found = []
        self._summary = {}

        self._roi_dict = {
            "y_min": roi_y_min,
            "x_min": roi_x_min,
            "y_max": roi_y_max,
            "x_max": roi_x_max,
        }

        self._show_boxes = show_boxes
        self._last_detection = None
        self._image_width = None
        self._image_height = None
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file

    def process_image(self, image):
        """Process an image."""
        self._image_width, self._image_height = Image.open(
            io.BytesIO(bytearray(image))
        ).size
        self._state = None
        self._objects = []  # The parsed raw data
        self._targets_found = []
        self._summary = {}
        saved_image_path = None

        try:
            predictions = self._dsobject.detect(image)
        except ds.DeepstackException as exc:
            _LOGGER.error("Deepstack error : %s", exc)
            return

        self._summary = ds.get_objects_summary(predictions)
        self._objects = get_objects(predictions, self._image_width, self._image_height)
        self._targets_found = [
            obj
            for obj in self._objects
            if (obj["name"] or obj["object_type"] in self._targets)
            and (obj["confidence"] > self._confidence)
            and (object_in_roi(self._roi_dict, obj["centroid"]))
        ]

        self._state = len(self._targets_found)
        if self._state > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)

        if self._save_file_folder and self._state > 0:
            saved_image_path = self.save_image(
                image, self._targets_found, self._save_file_folder,
            )

        # Fire events
        for target in self._targets_found:
            target_event_data = target.copy()
            target_event_data[ATTR_ENTITY_ID] = self.entity_id
            if saved_image_path:
                target_event_data[SAVED_FILE] = saved_image_path
            self.hass.bus.fire(EVENT_OBJECT_DETECTED, target_event_data)

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def device_state_attributes(self) -> Dict:
        """Return device specific state attributes."""
        attr = {}
        attr["targets"] = self._targets
        for target in self._targets:
            attr[f"ROI {target} count"] = len(
                [t for t in self._targets_found if t["name"] == target]
            )
            attr[f"ALL {target} count"] = len(
                [t for t in self._objects if t["name"] == target]
            )
        if self._last_detection:
            attr["last_target_detection"] = self._last_detection
        if self._custom_model:
            attr["custom_model"] = self._custom_model
        attr["summary"] = self._summary
        if self._save_file_folder:
            attr[CONF_SAVE_FILE_FOLDER] = str(self._save_file_folder)
        if self._save_timestamped_file:
            attr[CONF_SAVE_TIMESTAMPTED_FILE] = self._save_timestamped_file
        return attr

    def save_image(self, image, targets, directory) -> str:
        """Draws the actual bounding box of the detected objects.

        Returns: saved_image_path, which is the path to the saved timestamped file if configured, else the default saved image.
        """
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Deepstack unable to process image, bad data")
            return
        draw = ImageDraw.Draw(img)

        roi_tuple = tuple(self._roi_dict.values())
        if roi_tuple != DEFAULT_ROI and self._show_boxes:
            draw_box(
                draw, roi_tuple, img.width, img.height, text="ROI", color=GREEN,
            )

        for obj in targets:
            if not self._show_boxes:
                break
            name = obj["name"]
            confidence = obj["confidence"]
            box = obj["bounding_box"]
            centroid = obj["centroid"]
            box_label = f"{name}: {confidence:.1f}%"

            draw_box(
                draw,
                (box["y_min"], box["x_min"], box["y_max"], box["x_max"]),
                img.width,
                img.height,
                text=box_label,
                color=RED,
            )

            # draw bullseye
            draw.text(
                (centroid["x"] * img.width, centroid["y"] * img.height),
                text="X",
                fill=RED,
            )

        # Save images, returning the path of saved image as str
        latest_save_path = (
            directory / f"{get_valid_filename(self._name).lower()}_latest.jpg"
        )
        _LOGGER.info("Deepstack saved file %s", latest_save_path)
        img.save(latest_save_path)
        saved_image_path = latest_save_path

        if self._save_timestamped_file:
            timestamp_save_path = directory / f"{self._name}_{self._last_detection}.jpg"
            img.save(timestamp_save_path)
            _LOGGER.info("Deepstack saved file %s", timestamp_save_path)
            saved_image_path = timestamp_save_path
        return str(saved_image_path)
