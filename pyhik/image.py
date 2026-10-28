"""
pyhik.image
~~~~~~~~~~~
Discovery of a device's image settings from ISAPI.

A Hikvision device describes its adjustable image settings in
``/ISAPI/Image/channels/<n>/capabilities``: an element with an ``opt``
attribute is a choice, one with ``min``/``max`` is a range, and an ``opt`` of
``true,false`` is a switch. The current values live in
``/ISAPI/Image/channels/<n>``. This module turns those two documents into a
flat list of :class:`ImageSetting` objects. It does no I/O and uses only the
standard library.

Settings are addressed by their slash-separated path, e.g.
``Exposure/OverexposeSuppress/enabled``, with XML namespaces stripped. When an
element is repeated, only its first occurrence is used.

Derived in part from https://github.com/JoshADC/hikvision_isapi
(MIT License), Copyright (c) JoshADC and contributors.
Licensed under the MIT license.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Dict, List, Optional, Set, Union
import xml.etree.ElementTree as ET

_LOGGER = logging.getLogger(__name__)

# Top-level elements of the ImageChannel document that identify the channel
# rather than describe an adjustable setting.
STRUCTURAL_PATHS = frozenset({"id", "enabled", "videoInputID"})


class ImageSettingType(Enum):
    """Kind of control an image setting maps to."""

    SWITCH = "switch"
    NUMBER = "number"
    SELECT = "select"


@dataclass
class ImageSetting:
    """One adjustable image setting reported by a device.

    Attributes:
        path: Slash-separated element path, namespaces stripped.
        type: Switch, number (range) or select (choice).
        options: Allowed raw values for a select; empty otherwise.
        min_value: Lower bound of a number.
        max_value: Upper bound of a number.
        value: Current value as the device reports it, or None if the
            device does not report one right now.
        linked_enabled_path: For a select that also controls an ``enabled``
            flag (e.g. ``BLC/BLCMode`` -> ``BLC/enabled``), the path of that
            flag. The separate switch for it is not reported.
        off_value: For a linked select, the option that means "disabled".
    """

    path: str
    type: ImageSettingType
    options: List[str] = field(default_factory=list)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    value: Optional[str] = None
    linked_enabled_path: Optional[str] = None
    off_value: Optional[str] = None


def _strip_ns(tag: str) -> str:
    """Strip an XML namespace: ``{http://...}Tag`` -> ``Tag``."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _parse(xml: Union[str, bytes]) -> ET.Element:
    """Parse an XML document given as text or bytes."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    return ET.fromstring(xml)


def parse_image_values(xml: Union[str, bytes]) -> Dict[str, str]:
    """Flatten an ImageChannel document into ``{path: value}``.

    Only leaf elements that carry text are included. For a repeated element
    the first occurrence wins, like :func:`find_by_path` and the writes.
    """
    values: Dict[str, str] = {}
    _collect_values(_parse(xml), "", values)
    return values


def _collect_values(element: ET.Element, parent: str, out: Dict[str, str]) -> None:
    for child in element:
        path = "%s/%s" % (parent, _strip_ns(child.tag)) if parent else _strip_ns(child.tag)
        if len(child):
            _collect_values(child, path, out)
        elif child.text and child.text.strip():
            out.setdefault(path, child.text.strip())


def parse_image_capabilities(
    capabilities_xml: Union[str, bytes],
    values_xml: Optional[Union[str, bytes]] = None,
) -> List[ImageSetting]:
    """Build the list of image settings a device supports.

    Args:
        capabilities_xml: Body of ``/ISAPI/Image/channels/<n>/capabilities``.
        values_xml: Optional body of ``/ISAPI/Image/channels/<n>``. When
            given, ``ImageSetting.value`` comes from it. Settings missing
            from it fall back to the (non-empty) default in the
            capabilities document, otherwise None.

    Returns:
        Settings in document order. Everything with an ``opt`` or
        ``min``/``max`` attribute is returned, except the channel's own
        identifying elements (:data:`STRUCTURAL_PATHS`); callers decide
        which of the rest they want to expose.
    """
    settings: List[ImageSetting] = []
    _walk(_parse(capabilities_xml), "", settings)

    current: Dict[str, str] = {}
    if values_xml is not None:
        current = parse_image_values(values_xml)
        for setting in settings:
            if setting.path in current:
                setting.value = current[setting.path]

    _merge_enabled_mode(settings, current, values_xml is not None)
    return settings


def _walk(
    element: ET.Element,
    parent: str,
    out: List[ImageSetting],
    seen: Optional[Set[str]] = None,
) -> None:
    seen = set() if seen is None else seen
    for child in element:
        tag = _strip_ns(child.tag)
        path = "%s/%s" % (parent, tag) if parent else tag
        if path in seen:
            # A repeated element (for example a list entry): only the first is
            # used, so that every path is reported once.
            continue
        seen.add(path)

        if path not in STRUCTURAL_PATHS:
            setting = _setting_from(child, path)
            if setting is not None:
                out.append(setting)

        if len(child):
            _walk(child, path, out, seen)


def _setting_from(child: ET.Element, path: str) -> Optional[ImageSetting]:
    default = (child.text or "").strip() or None
    opt = child.attrib.get("opt")
    if opt is not None:
        options = [o.strip() for o in opt.split(",") if o.strip()]
        if not options:
            return None
        if {o.lower() for o in options} == {"true", "false"}:
            return ImageSetting(path, ImageSettingType.SWITCH, value=default)
        return ImageSetting(
            path, ImageSettingType.SELECT, options=options, value=default
        )

    min_val = child.attrib.get("min")
    max_val = child.attrib.get("max")
    if min_val is not None and max_val is not None:
        try:
            return ImageSetting(
                path,
                ImageSettingType.NUMBER,
                min_value=float(min_val),
                max_value=float(max_val),
                value=default,
            )
        except ValueError:
            _LOGGER.debug("Ignoring %s: non-numeric range %r-%r", path, min_val, max_val)
    return None


def _is_off(option: str) -> bool:
    """Return True if a mode option means "off" ("close" in any letter case).

    When such a mode has an ``enabled`` sibling (e.g. BLC/enabled +
    BLC/BLCMode) the device UI shows them as one control, so they are
    reported as one linked setting.
    """
    return option.lower() == "close"


def _merge_enabled_mode(
    settings: List[ImageSetting], current: Dict[str, str], have_values: bool
) -> None:
    """Fold ``<parent>/enabled`` into a sibling mode select with an off option.

    The mode element is usually absent from the current values while the
    feature is disabled, so a linked select reports its off value in that
    case.
    """
    switches: Dict[str, ImageSetting] = {}
    selects: Dict[str, ImageSetting] = {}
    for setting in settings:
        parent, _, leaf = setting.path.rpartition("/")
        if not parent:
            continue
        if setting.type is ImageSettingType.SWITCH and leaf.lower() == "enabled":
            switches[parent] = setting
        elif setting.type is ImageSettingType.SELECT and any(
            _is_off(o) for o in setting.options
        ):
            # If several qualify, the first in document order is linked.
            selects.setdefault(parent, setting)

    for parent, switch in switches.items():
        select = selects.get(parent)
        if select is None:
            continue
        # Kept in the device's spelling, which is what it expects back.
        off = next(o for o in select.options if _is_off(o))
        select.linked_enabled_path = switch.path
        select.off_value = off
        if have_values and current.get(switch.path, "").lower() != "true":
            select.value = off
        settings.remove(switch)
