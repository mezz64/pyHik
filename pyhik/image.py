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
import html
import logging
import re
from typing import Dict, List, Optional, Set, Tuple, Union
from xml.sax.saxutils import escape
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


# ---------------------------------------------------------------------------
# Writing
#
# Most devices require the complete ImageChannel document on every PUT, with
# the exact XML they sent (including repeated ``xmlns`` declarations on child
# elements), so a write is "read the document, change the target values as
# text, send it back". ElementTree is used only to *find* values; the PUT body
# is the original string with the target text replaced.
# ---------------------------------------------------------------------------

# Conflict error codes the device returns -> settings to switch off first.
# The device validates a change against its CURRENT state, not the PUT body,
# so the blocker must be switched off in its own PUT before retrying.
CONFLICT_RESOLUTIONS: Dict[str, Dict[str, str]] = {
    "WDRNotDisable": {"WDR/mode": "close"},
    "MutexWithWDR": {"WDR/mode": "close"},
    "HLCNotDisable": {"HLC/enabled": "false"},
    "BLCNotDisable": {"BLC/enabled": "false"},
}

# MutexWithWDR is returned both when enabling something while WDR is on and
# when enabling WDR while something else is on. When the setting being
# changed is WDR itself, the blockers are BLC and HLC.
_WDR_REVERSE_RESOLUTION: Dict[str, str] = {
    "BLC/enabled": "false",
    "HLC/enabled": "false",
}


# Conflicts the device reports with HTTP 403 that are explained when they
# cannot be resolved (for example because the retries ran out).
CONFLICT_DESCRIPTIONS: Dict[str, str] = {
    "HLCMutexWDROrDehaze": (
        "WDR or defog cannot be turned on while HLC (Highlight Compensation) "
        "is on. Turn HLC off first."
    ),
    "WDROrDehazeMutexHLC": (
        "HLC (Highlight Compensation) cannot be turned on while WDR or defog "
        "is on. Turn WDR and defog off first."
    ),
}

# ``sub_status`` of a failed write whose reply carries no ``subStatusCode``.
# "ok" is used only for a successful write.
NO_SUB_STATUS = "noSubStatus"

# Sub status codes of a 403 that really are about the user's rights (devices
# that send no sub status code count too).
PRIVILEGE_SUB_STATUSES = frozenset({"lowPrivilege", NO_SUB_STATUS, ""})


@dataclass
class ImageWriteResult:
    """Outcome of writing an image setting.

    Attributes:
        success: True if the device accepted the change.
        status_code: HTTP status of the last request (0 if none was sent).
        sub_status: The device's ``subStatusCode`` (e.g. ``WDRNotDisable``,
            ``notSupport``), ``ok`` for a successful write that sent none, or
            a pyhik-generated code such as ``pathNotFound``,
            ``invalidResponse`` or ``noSubStatus`` (a failure whose reply has
            no ``subStatusCode``).
        also_changed: Other settings that were changed to make room for the
            requested one, as ``{path: new value}`` (for example
            ``{"HLC/enabled": "false"}`` when turning WDR on required HLC to be
            turned off). Empty if nothing else was touched. It can be set on a
            failed result too, when a blocker was switched off but the
            requested change was still refused.
    """

    success: bool
    status_code: int
    sub_status: str
    also_changed: Dict[str, str] = field(default_factory=dict)

    @property
    def error_description(self) -> str:
        """Human-readable description of a failure."""
        if self.sub_status in CONFLICT_DESCRIPTIONS:
            # Devices answer 403 for these too, but it is not a permission problem.
            return "%s (%s)" % (CONFLICT_DESCRIPTIONS[self.sub_status], self.sub_status)
        detail = self.sub_status
        if detail == NO_SUB_STATUS:
            detail = "HTTP %d" % self.status_code
        if self.status_code == 403 and self.sub_status in PRIVILEGE_SUB_STATUSES:
            return (
                "%s - permission denied. Check that the user has remote "
                "configuration write privileges on the device." % detail
            )
        return detail


# HLCMutexWDROrDehaze (WDR or defog switched on while HLC is on) and
# WDROrDehazeMutexHLC (HLC switched on while WDR or defog is on): highlight
# compensation cannot be on together with WDR or defog. Only changes to one of
# those three settings are resolved; for any other path the device is
# complaining about a state the caller did not create.
_HLC_BLOCKERS: Dict[str, str] = {
    "WDR/mode": "close",
    "Dehaze/DehazeMode": "close",
}
_HLC_MUTEX_CODES = frozenset({"HLCMutexWDROrDehaze", "WDROrDehazeMutexHLC"})
# Turning HLC on: its enabled flag, or its mode on firmware where the mode is
# linked to that flag (HLC/HLCMode with an off value, like BLC/BLCMode).
_HLC_ON_PATHS = frozenset({"hlc/enabled", "hlc/hlcmode"})
_HLC_MUTEX_PATHS = _HLC_ON_PATHS | {"wdr/mode", "dehaze/dehazemode"}


def conflict_resolution(sub_status: str, path: str) -> Optional[Dict[str, str]]:
    """Return the settings to switch off to resolve a conflict, if known."""
    if sub_status in _HLC_MUTEX_CODES:
        if path.lower() not in _HLC_MUTEX_PATHS:
            return None
        if path.lower() in _HLC_ON_PATHS:
            return dict(_HLC_BLOCKERS)
        return {"HLC/enabled": "false"}
    resolution = CONFLICT_RESOLUTIONS.get(sub_status)
    if resolution is None:
        return None
    if sub_status == "MutexWithWDR" and path.lower() in {p.lower() for p in resolution}:
        return dict(_WDR_REVERSE_RESOLUTION)
    return dict(resolution)


# ResponseStatus statusCode values that mean the change was applied: 1 is OK,
# 7 is "Reboot Required" (applied, takes effect after a reboot).
#
# Hikvisio says changing "power line frequency" needs a reboot to take effect.
# In current firmware, this might be the only image change that needs a reboot.
_APPLIED_STATUS_CODES = frozenset({"1", "7"})


def parse_response_status(content: bytes, http_status: int) -> ImageWriteResult:
    """Parse a ``ResponseStatus`` document into an :class:`ImageWriteResult`.

    A 200 with an empty body is taken as success, as is a "Reboot Required"
    status (the change was applied; ``sub_status`` says ``rebootRequired``).
    A failure without a ``subStatusCode`` gets ``noSubStatus``.

    A 401 is not handled here; the client raises ``ISAPIAuthError`` for it
    before parsing the body.
    """
    if http_status == 200 and not content.strip():
        return ImageWriteResult(True, http_status, "ok")
    try:
        root = _parse(content)
    except ET.ParseError:
        return ImageWriteResult(False, http_status, "invalidResponse")
    status = _child_text(root, "statusString")
    code = _child_text(root, "statusCode")
    success = http_status == 200 and (status == "OK" or code in _APPLIED_STATUS_CODES)
    # "ok" only stands for a successful write. An error page that happens to
    # be well-formed XML has no subStatusCode either, and must not read as ok.
    sub_status = _child_text(root, "subStatusCode") or (
        "ok" if success else NO_SUB_STATUS
    )
    return ImageWriteResult(success, http_status, sub_status)


def _child_text(element: ET.Element, tag: str) -> str:
    for child in element:
        if _strip_ns(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def find_by_path(root: ET.Element, path: str) -> Optional[ET.Element]:
    """Find an element by slash-separated path, ignoring namespaces."""
    current = root
    for part in path.split("/"):
        for child in current:
            if _strip_ns(child.tag) == part:
                current = child
                break
        else:
            return None
    return current


# Markup in a document: comments, declarations and CDATA (group 2 is None), or
# an element tag: group 1 is "/" for a closing tag, group 2 the name, group 3
# "/" for a self-closing tag. Quoted attribute values may contain ">".
_TAG = re.compile(
    r"<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>|<![^>]*>"
    r"|<(/?)([^\s/>]+)(?:[^>\"']|\"[^\"]*\"|'[^']*')*?(/?)>",
    re.S,
)


@dataclass
class _ElementSpan:
    """Where an element is in a document's text."""

    start: int  # start of the opening tag
    text_start: int  # end of the opening tag
    text_end: int  # start of the closing tag (text_start if self-closing)
    end: int  # end of the closing tag


def _locate(xml: str, path: str) -> Optional[_ElementSpan]:
    """Find an element of a document's text by its path below the root.

    Follows the same rule as :func:`find_by_path`: the first child with the
    wanted name at each level, namespace prefixes ignored. So ``BLC/enabled``
    never matches an ``enabled`` elsewhere, nor one nested deeper in ``BLC``.
    """
    parts = path.split("/")
    depth = 0  # depth of the element being opened or closed; the root is 1
    matched = 0  # path parts matched; the deepest is at depth matched + 1
    target: Optional[Tuple[int, int, int]] = None  # (start, text_start, depth)
    for tag in _TAG.finditer(xml):
        name = tag.group(2)
        if name is None:
            continue
        if tag.group(1):  # closing tag
            if target is not None and depth == target[2]:
                return _ElementSpan(target[0], target[1], tag.start(), tag.end())
            if target is None and matched and depth == matched + 1:
                return None  # the first match at this level has no such child
            depth -= 1
            continue
        depth += 1
        self_closing = bool(tag.group(3))
        if (
            target is None
            and depth == matched + 2
            and name.rsplit(":", 1)[-1] == parts[matched]
        ):
            matched += 1
            if matched == len(parts):
                if self_closing:
                    return _ElementSpan(tag.start(), tag.end(), tag.end(), tag.end())
                target = (tag.start(), tag.end(), depth)
            elif self_closing:
                return None
        if self_closing:
            depth -= 1
    return None


def raw_replace(xml: str, path: str, old_value: str, new_value: str) -> str:
    """Replace the text of the element at ``path`` (below the root) in raw XML.

    Only that exact element is edited: for ``BLC/enabled`` the ``enabled``
    directly inside the first ``BLC``, never ``HLC/enabled`` or an ``enabled``
    nested deeper. The current text must equal ``old_value`` (after XML
    unescaping); ``new_value`` is XML-escaped. Returns the input unchanged if
    the element is missing, self-closing, has child elements or holds other
    text.
    """
    span = _locate(xml, path)
    if span is None or span.text_start == span.end:  # missing or self-closing
        return xml
    text = xml[span.text_start : span.text_end]
    if "<" in text or html.unescape(text) != old_value:
        return xml
    return xml[: span.text_start] + escape(new_value) + xml[span.text_end :]


def raw_insert_after(xml: str, after_path: str, new_path: str, value: str) -> str:
    """Insert a missing element right after a sibling, within its parent.

    Used when a mode element (e.g. ``BLCMode``) is absent while its feature
    is disabled. ``after_path`` is below the root, so it is a bare element
    name when the document is itself the parent (a sub-endpoint document).
    Returns the input unchanged if the sibling is not found.
    """
    span = _locate(xml, after_path)
    if span is None:
        _LOGGER.warning("Could not insert %s after %s", new_path, after_path)
        return xml
    new_tag = new_path.split("/")[-1]
    element = "\n<%s>%s</%s>" % (new_tag, escape(value), new_tag)
    return xml[: span.end] + element + xml[span.end :]


def apply_image_changes(
    xml: str, changes: Dict[str, str]
) -> Tuple[str, List[str]]:
    """Apply ``{path: value}`` to an ImageChannel document as text edits.

    Returns the edited document and the paths that could not be applied
    (absent from the document, or not editable as plain element text).
    Values that already match are left alone.
    """
    tree = ET.fromstring(xml.encode("utf-8"))
    edited = xml
    missing: List[str] = []
    for path, value in changes.items():
        element = find_by_path(tree, path)
        if element is None:
            missing.append(path)
            continue
        old = element.text or ""
        if old == value:
            continue
        updated = raw_replace(edited, path, old, value)
        if updated == edited:
            missing.append(path)
            continue
        edited = updated
        element.text = value
    return edited, missing


def apply_enable_and_mode(
    xml: str, enabled_path: str, mode_path: str, mode_value: str
) -> Optional[str]:
    """Set ``enabled_path`` to true and ``mode_path`` to ``mode_value``.

    Inserts the mode element after the enabled flag when the device omits it
    while the feature is off. Returns None if the enabled flag is not in the
    document.
    """
    tree = ET.fromstring(xml.encode("utf-8"))
    enabled = find_by_path(tree, enabled_path)
    if enabled is None:
        return None
    edited = xml
    if (enabled.text or "") != "true":
        edited = raw_replace(edited, enabled_path, enabled.text or "", "true")
    mode = find_by_path(tree, mode_path)
    if mode is not None:
        if (mode.text or "") != mode_value:
            edited = raw_replace(edited, mode_path, mode.text or "", mode_value)
    else:
        edited = raw_insert_after(edited, enabled_path, mode_path, mode_value)
    return edited
