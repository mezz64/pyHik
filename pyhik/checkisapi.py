"""Check pyhik's image settings support against a real camera or NVR.

Not a unit test: unit tests must run without hardware.

Run it with the pyhik you want to check installed:

    export CAMERA_HOST=192.168.1.100
    export CAMERA_USERNAME=admin
    export CAMERA_PASSWORD='passwd123'
    python -m pyhik checkisapi                  # read-only, always safe

The host, port, user name, HTTPS and certificate check can also be given as --host, --port,
--username, --ssl and --no-verify, which win over the environment variables. The password is
only read from the environment.

What to run:

    python -m pyhik checkisapi                           # read-only, lists every setting and its path
    python -m pyhik checkisapi --write                   # change Color/brightnessLevel by one step and put it back
    python -m pyhik checkisapi --write --path WDR/mode   # a switch is flipped, a select gets another option
    python -m pyhik checkisapi --conflicts               # WDR / HLC / BLC / defog conflict matrix
    python -m pyhik checkisapi --bad-password            # a wrong password, tried by each client
    python -m pyhik checkisapi --all                     # all of the above
    python -m pyhik checkisapi -v                        # also show pyhik's debug log (the retries)

--write changes the setting Color/brightnessLevel by one step (or the first numeric
setting if the device has no brightness) and restores it. To choose the setting yourself
take a path from the list printed by the read-only run. --path is only used together
with --write and works for every kind of setting:

    python -m pyhik checkisapi --write --path Color/contrastLevel          # a number: one step
    python -m pyhik checkisapi --write --path IrcutFilter/IrcutFilterType  # a select: another option
    python -m pyhik checkisapi --write --path <a switch path>              # a switch: on <-> off

A setting that was switched off to make room for the one under test (WDR, BLC, HLC and
defog exclude each other) is put back too and listed.

One channel is checked per run. Without --channel, the first listed channel is used. On an NVR,
give the channel ID of the attached camera, for example the camera on channel 3:

    python -m pyhik checkisapi --channel 3
    python -m pyhik checkisapi --channel 3 --write --path Color/contrastLevel

An NVR is detected from the device's own type (NVR or DVR). A recorder that reports the
type of a camera (IPC), or does not answer the device information request, is detected by
its camera image endpoints failing while the proxy endpoints work. Its attached cameras
are then used through the proxy endpoints. --proxy and --no-proxy override the detection.

Optional: CAMERA_PORT (80, or 443 with HTTPS), CAMERA_SSL=1, CAMERA_VERIFY_SSL=0.

Everything that changes a value puts it back afterwards (even after an error), and
the script says so when it could not. It stops at the first authentication error
because repeated failures can lock the account. --bad-password tries a wrong password
through each of the two clients (ISAPIClient and HikCamera); at least two failed
logins, more if a client retries; do not use it on an account that is close to being locked.
"""

import argparse
import logging
import os
import re
import sys

import pyhik
from pyhik.image import ImageSettingType
from pyhik.isapi import (
    ISAPIAuthError,
    ISAPIClient,
    ISAPIConnectionError,
    ISAPIError,
    ISAPINotSupportedError,
)


class Report:
    """Collects the outcome of every check so the script can end with a summary."""

    def __init__(self, out=print):
        self.out = out
        self.results = []  # (name, "PASS" | "FAIL" | "SKIP", detail)

    def add(self, name, status, detail=""):
        self.results.append((name, status, detail))
        self.out("   -> %s%s" % (status, (": " + detail) if detail else ""))

    @property
    def failed(self):
        return any(status == "FAIL" for _, status, _ in self.results)

    def summary(self):
        self.out("")
        self.out("summary")
        for name, status, detail in self.results:
            self.out("   %-5s %s%s" % (status, name, (" (" + detail + ")") if detail else ""))
        self.out("   overall: %s" % ("FAILED" if self.failed else "ok"))


def _flag(name, default):
    return os.environ.get(name, str(int(default))).strip().lower() in ("1", "true", "yes")


def settings_from_env(password=None, host=None, port=None, username=None, ssl=None, verify_ssl=None):
    """The connection settings. What is given here wins over the environment.

    ssl=True means HTTPS and verify_ssl=False means do not check the certificate; None means
    the CAMERA_SSL / CAMERA_VERIFY_SSL variables decide. Without a port, HTTPS uses 443 and
    HTTP 80.
    """
    missing = []
    if not (host or os.environ.get("CAMERA_HOST")):
        missing.append("CAMERA_HOST (or --host)")
    if not os.environ.get("CAMERA_PASSWORD"):
        missing.append("CAMERA_PASSWORD")
    if missing:
        sys.exit("Set these first: " + ", ".join(missing)
                 + " (optional: CAMERA_USERNAME or --username, CAMERA_PORT or --port,"
                 " CAMERA_SSL or --ssl, CAMERA_VERIFY_SSL or --no-verify)")
    use_ssl = bool(ssl) or _flag("CAMERA_SSL", False)
    return dict(
        host=host or os.environ["CAMERA_HOST"],
        port=port if port is not None else int(os.environ.get("CAMERA_PORT", "443" if use_ssl else "80")),
        username=username or os.environ.get("CAMERA_USERNAME", "admin"),
        password=password if password is not None else os.environ["CAMERA_PASSWORD"],
        ssl=use_ssl,
        verify_ssl=False if verify_ssl is False else _flag("CAMERA_VERIFY_SSL", True),
    )


def remember_device_info(client):
    """Make client.get_device_info() ask the device once per run, whether it answers or not.

    pyhik keeps an answer but not a failure, so on a device whose device info fails each of
    get_device_model(), get_device_type() and get_firmware_version() sent a request of its
    own and detection one more.
    """
    ask = client.get_device_info
    seen = {}

    def get_device_info():
        if "error" in seen:
            raise seen["error"]
        if "info" not in seen:
            try:
                seen["info"] = ask()
            except Exception as err:
                seen["error"] = err
                raise
        return seen["info"]

    client.get_device_info = get_device_info
    return client


def client_from_env(password=None, **overrides):
    return remember_device_info(ISAPIClient(**settings_from_env(password, **overrides)))


def _describe(err):
    return "%s: %s (status=%s, sub_status=%s)" % (
        type(err).__name__, err, err.status_code, err.sub_status,
    )


def pick_number(settings, path=None):
    """The setting to change: the given path, else brightness, else any numeric setting."""
    by_path = {s.path: s for s in settings}
    if path:
        return by_path.get(path)
    if "Color/brightnessLevel" in by_path:
        return by_path["Color/brightnessLevel"]
    return next((s for s in settings if s.type is ImageSettingType.NUMBER and s.value), None)


def other_value(target):
    """A value different from the current one to try, or None when there is none.

    A number moves one step (down at its upper limit), a switch flips and a select
    takes the first other option.
    """
    original = target.value
    if original is None:
        return None
    if target.type is ImageSettingType.NUMBER:
        try:
            return str(int(original) + 1 if int(original) < int(target.max_value) else int(original) - 1)
        except (TypeError, ValueError):
            return None
    if target.type is ImageSettingType.SWITCH:
        return "false" if original.lower() == "true" else "true"
    return next((o for o in target.options if o != original), None)


def suggest_paths(settings, out):
    """Name a few switches and selects the user can try with --path."""
    for kind, label in ((ImageSettingType.SWITCH, "switch"), (ImageSettingType.SELECT, "select")):
        paths = [s.path for s in settings if s.type is kind and s.value is not None][:3]
        if paths:
            out("   to try a %s too: --write --path %s" % (label, " | ".join(paths)))


def _short(err):
    """The exception name, with the HTTP status and the device's sub status when it has them."""
    status = getattr(err, "status_code", None)
    sub_status = getattr(err, "sub_status", None)
    detail = ", ".join(x for x in (str(err), "status=%s" % status if status else "",
                                    "sub_status=%s" % sub_status if sub_status else "") if x)
    return "%s: %s" % (type(err).__name__, detail)


def _device_line(client, out):
    """Print what a bug report needs to know about the device (no serial number)."""
    out("pyhik %s from %s" % (pyhik.__version__, os.path.dirname(pyhik.__file__)))
    parts = []
    for label, getter in (
        ("model", "get_device_model"),
        ("type", "get_device_type"),
        ("firmware", "get_firmware_version"),
    ):
        try:
            parts.append("%s=%s" % (label, getattr(client, getter)() or "?"))
        except ISAPIAuthError:
            raise  # the run stops here: every further request would be one more failed login
        except Exception as err:  # anything else is not worth failing the run for
            parts.append("%s=? (%s)" % (label, _short(err)))
    out("device: " + ", ".join(parts))


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def check_read(client, report, channel, proxy, out):
    """Channels, settings and values. Returns (channel, settings) or None."""
    out("1. channels")
    channels = client.get_image_channels(proxy=proxy)
    for c in channels:
        out("   id=%s enabled=%s input_port=%s" % (c.id, c.enabled, c.input_port))
    if not channels:
        report.add("channels", "FAIL", "none listed")
        return None
    report.add("channels", "PASS", "%d listed" % len(channels))
    channel = channel if channel is not None else channels[0].id

    out("2. settings of channel %s" % channel)
    settings = client.get_image_settings(channel, proxy=proxy)
    kinds = {}
    for s in settings:
        kinds[s.type.value] = kinds.get(s.type.value, 0) + 1
    out("   %d settings: %s" % (len(settings), kinds))
    for s in settings:
        extra = ("options=%s" % s.options) if s.options else (
            "range=%s-%s" % (s.min_value, s.max_value) if s.min_value is not None else "")
        link = " (linked to %s)" % s.linked_enabled_path if s.linked_enabled_path else ""
        out("   %-8s %-60s %-10s %s%s" % (s.type.value, s.path, s.value, extra, link))
    report.add("settings", "PASS" if settings else "FAIL", "%d found" % len(settings))

    out("3. current values")
    values = client.get_image_values(channel, proxy=proxy)
    out("   %d values read" % len(values))
    report.add("values", "PASS" if values else "FAIL", "%d read" % len(values))
    return channel, settings


def current_value(setting, values):
    """The value of a setting in values, as get_image_settings() reports it.

    A linked select that is switched off reports its off value: a device normally leaves
    the mode element out while the feature is off.
    """
    flag = setting.linked_enabled_path
    if flag and (values.get(flag) or "").lower() != "true":
        return setting.off_value
    return values.get(setting.path)


def _is_on(value):
    return str(value).lower() not in ("false", "close")


def restore_steps(client, channel, proxy, settings, before, paths):
    """The writes that put these paths back to their values in before: [(wanted, write)].

    wanted is the {path: value} a write puts back and write() sends it. A linked select
    (BLC and its mode) is one write for both of its paths, through the linked setter when
    it was on, so that the mode comes back with the flag. What was off comes first:
    switching a setting on while one it conflicts with is still on is refused, or makes
    pyhik switch the other one off.
    """
    linked = {}
    for setting in settings:
        if setting.linked_enabled_path:
            linked[setting.path.lower()] = linked[setting.linked_enabled_path.lower()] = setting
    steps = {}  # wanted paths -> (was on, wanted, write)
    for path in paths:
        select = linked.get(path.lower())
        if select is None:
            wanted = {path: before.get(path)}
            on = _is_on(wanted[path])

            def write(path=path):
                return client.set_image_setting(path, before[path], channel, proxy=proxy)
        else:
            flag, mode = select.linked_enabled_path, before.get(select.path)
            wanted = {flag: before.get(flag)}
            if mode is not None:
                wanted[select.path] = mode
            on = _is_on(wanted[flag])
            if on and mode not in (None, select.off_value):
                def write(select=select, mode=mode):
                    return client.set_image_linked_setting(select, mode, channel, proxy=proxy)
            else:
                def write(wanted=wanted):
                    return client.put_image_settings(wanted, channel, proxy=proxy)
        steps.setdefault(tuple(wanted), (on, wanted, write))
    return [(wanted, write) for _, wanted, write in sorted(steps.values(), key=lambda step: step[0])]


def put_back(client, report, channel, proxy, settings, before, paths, out, skip=False,
             anything_else=False):
    """Put the settings at these paths back to their values in before, and check them.

    Only what differs from before is written. With anything_else=True that is every
    setting that differs, not just these paths: for a write that raised before it
    could say what it had switched off to make room. Without it, what else differs is
    listed and left alone: this check did not change it, so it is not its to write.

    Returns an ISAPIAuthError or None. It never raises, so it cannot hide the error that
    made the restore necessary, and one setting that cannot be put back does not stop
    the others. After an authentication error, or with skip=True, nothing more is sent:
    repeated failures can lock the account. What was not put back is reported with the
    value to set by hand.
    """
    auth_error, now = None, None
    if not skip:
        try:
            now = client.get_image_values(channel, proxy=proxy)
        except ISAPIAuthError as err:
            auth_error = err
        except Exception as err:  # the paths are written back all the same
            out("   restore: could not read what the device has now (%s)" % _short(err))
    paths = list(paths)
    if anything_else and now is not None:
        paths += [path for path, value in before.items() if now.get(path) != value]
    elif anything_else:
        out("   restore: NOT CHECKED what else was changed before the error, compare WDR, HLC, BLC "
            "and defog with the values listed above")
        report.add("restore after the error", "FAIL",
                   "could not see what else was changed, check WDR, HLC, BLC and defog by hand")

    todo = restore_steps(client, channel, proxy, settings, before, paths)
    known = {path for wanted, _ in todo for path in wanted}
    if now is not None:
        todo = [(wanted, write) for wanted, write in todo
                if any(now.get(path) != value for path, value in wanted.items())]
        # What is not in the document now comes back with the setting it belongs to
        # (a level with its mode), so it is written after that one.
        todo.sort(key=lambda step: any(path not in now for path in step[0]))
        if not todo:
            out("   restore: nothing differs from before, nothing to put back")

    steps = []  # (wanted, what went wrong or None)
    for wanted, write in todo:
        problem = None
        if None in wanted.values():
            problem = "NOT RESTORED (its original value is not known)"
        elif skip or auth_error:
            problem = "NOT TRIED after the authentication error"
        else:
            try:
                result = write()
                if not result.success:
                    problem = "NOT RESTORED (refused: %s)" % result.error_description
            except ISAPIAuthError as err:
                auth_error, problem = err, "NOT RESTORED (%s)" % _short(err)
            except Exception as err:  # whatever it is, the other settings are still put back
                problem = "NOT RESTORED (%s)" % _short(err)
        steps.append((wanted, problem))

    after, unread = None, ""
    if steps and not (skip or auth_error):
        try:
            after = client.get_image_values(channel, proxy=proxy)
        except ISAPIAuthError as err:
            auth_error, unread = err, _short(err)
        except Exception as err:
            unread = _short(err)

    for wanted, problem in steps:
        label = ", ".join("%s -> %s" % item for item in wanted.items() if item[1] is not None) or ", ".join(wanted)
        if after is not None and all(after.get(path) == value for path, value in wanted.items()):
            out("   restore %s: restored" % label)
            continue
        if problem is None and after is not None:
            problem = "NOT RESTORED (the device now has %s)" % ", ".join(
                "%s=%s" % (path, after.get(path)) for path in wanted)
        elif problem is None:
            problem = "NOT CHECKED (could not read it back: %s)" % unread
        out("   restore %s: %s, set it back by hand" % (label, problem))
        report.add("restore of " + ", ".join(wanted), "FAIL", "check it and set it back by hand: " + label)

    last = after if after is not None else now
    others = [] if last is None else [
        "%s was %s, is %s" % (path, value, last.get(path))
        for path, value in before.items() if path not in known and last.get(path) != value]
    if others:
        out("   note: also different from before, and not changed by pyhik as far as this check "
            "knows: " + ", ".join(others))
    return auth_error


def check_write(client, report, channel, proxy, settings, path, out):
    """Change one setting (number, switch or select), read it back, put it back."""
    out("4. change a setting and put it back")
    target = pick_number(settings, path)
    if target is None and path:
        report.add("write", "SKIP", "no setting with the path %s (take one from the list)" % path)
        return
    if target is None or target.value is None:
        report.add("write", "SKIP", "no suitable numeric setting (use --path)")
        return
    new = other_value(target)
    if new is None:
        report.add("write", "SKIP", "%s has no other value to try" % target.path)
        return
    before = client.get_image_values(channel, proxy=proxy)
    out("   %s (%s): %s -> %s" % (target.path, target.type.value, target.value, new))
    result = None
    stop = False
    try:
        # The linked setter writes any setting; a linked select (BLC and its mode) needs it.
        result = client.set_image_linked_setting(target, new, channel, proxy=proxy)
        out("   result: %s" % result)
        if result.also_changed:
            out("   also switched off to make room: %s" % result.also_changed)
        got = current_value(target, client.get_image_values(channel, proxy=proxy))
        ok = result.success and got == new
        report.add("write", "PASS" if ok else "FAIL",
                   "read back OK" if ok else "read back %s" % got)
    except ISAPIAuthError:
        stop = True  # more requests would only add failed logins
        raise
    finally:
        # The setting under test, and the settings the write itself reported switching
        # off to make room (WDR, BLC, HLC and defog exclude each other). A write that
        # raised reported nothing, so then everything that differs is put back.
        paths = [target.path] + (list(result.also_changed) if result is not None else [])
        auth_error = put_back(client, report, channel, proxy, settings, before, paths, out,
                              skip=stop, anything_else=result is None)
    if auth_error:
        raise auth_error
    if not path:
        suggest_paths(settings, out)


class Feature:
    """A setting that can be on or off and may exclude others (WDR, HLC, BLC, defog)."""

    def __init__(self, name, variants, off, is_on, paths):
        self.name = name
        self.variants = variants  # [(label, apply)] ways to turn it on; apply() -> result
        self.off = off  # () -> result
        self.is_on = is_on  # (values) -> bool
        self.paths = paths  # what put_back() restores at the end


def build_features(client, channel, proxy, settings):
    """The conflict-prone settings this device offers, as Feature objects."""
    by_path = {s.path.lower(): s for s in settings}

    def put(path, value):
        return lambda: client.set_image_setting(path, value, channel, proxy=proxy)

    features = []

    def mode_feature(name, path):
        setting = by_path.get(path)
        if setting is None or "close" not in setting.options:
            return
        on_options = [o for o in setting.options if o != "close"]
        if not on_options:
            return
        features.append(Feature(
            name,
            [("%s=%s" % (name, o), put(setting.path, o)) for o in on_options],
            put(setting.path, "close"),
            lambda v, p=setting.path: v.get(p) not in (None, "close"),
            [setting.path],
        ))

    def flag_feature(name, mode_path, flag_path):
        # HLC and BLC have an enabled flag. Where the device also has a mode with an off
        # option, pyhik reports the two as one linked select; otherwise the flag is a switch.
        select = by_path.get(mode_path)
        if select is not None and select.linked_enabled_path and select.off_value is not None:
            modes = [o for o in select.options if o != select.off_value]
            mode = next((o for o in modes if o.lower() in ("auto", "center")), modes[0] if modes else None)
            if mode is not None:
                def set_mode(value):
                    return client.set_image_linked_setting(select, value, channel, proxy=proxy)

                features.append(Feature(
                    name, [("%s=%s" % (name, mode), lambda: set_mode(mode))], lambda: set_mode(select.off_value),
                    lambda v: v.get(select.linked_enabled_path) == "true",
                    [select.path, select.linked_enabled_path],
                ))
                return
        switch = by_path.get(flag_path)
        if switch is not None and switch.type is ImageSettingType.SWITCH:
            features.append(Feature(
                name, [(name, put(switch.path, "true"))], put(switch.path, "false"),
                lambda v: v.get(switch.path) == "true", [switch.path],
            ))

    mode_feature("WDR", "wdr/mode")
    mode_feature("defog", "dehaze/dehazemode")
    flag_feature("HLC", "hlc/hlcmode", "hlc/enabled")
    flag_feature("BLC", "blc/blcmode", "blc/enabled")
    return features


class ConflictCodes(logging.Handler):
    """Collects the refusal codes pyhik logs when it resolves a conflict."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.codes = []

    def emit(self, record):
        match = re.match(r"Conflict \((\w+)\)", record.getMessage())
        if match:
            self.codes.append(match.group(1))


def check_conflicts(client, report, channel, proxy, settings, out):
    """Try every "turn A on while B is on" combination and show how the device answers."""
    out("5. conflicts between WDR, HLC, BLC and defog (each is turned on while another is on)")
    features = build_features(client, channel, proxy, settings)
    if len(features) < 2:
        report.add("conflicts", "SKIP", "the device offers fewer than two of WDR, HLC, BLC and defog")
        return

    def read():
        return client.get_image_values(channel, proxy=proxy)

    before = read()
    out("   before: " + ", ".join(
        "%s=%s" % (p, before.get(p)) for f in features for p in f.paths))
    codes = ConflictCodes()
    logger = logging.getLogger("pyhik")
    old_level = logger.level
    logger.addHandler(codes)
    logger.setLevel(min(old_level or logging.INFO, logging.INFO))

    columns = [(f, label, apply) for f in features for label, apply in f.variants]
    cells = {}  # (row feature name, column label) -> (symbol, detail)
    made_room = {}  # what pyhik switched off by itself; it may be none of the features

    def tracked(write):
        result = write()
        made_room.update(result.also_changed)
        return result

    stop = False
    finished = False
    try:
        for row in features:
            for feature, label, apply in columns:
                if feature is row:
                    cells[(row.name, label)] = ("-", "")
                    continue
                for f in features:  # known starting point: everything off
                    tracked(f.off)
                first = row.variants[0]
                setup = tracked(first[1])
                if not setup.success:
                    cells[(row.name, label)] = ("?", "could not turn %s on first: %s" % (
                        first[0], setup.error_description))
                    continue
                codes.codes.clear()
                result = tracked(apply)
                values = read()
                row_still_on = row.is_on(values)
                seen = ", ".join(dict.fromkeys(codes.codes))
                if result.success and row_still_on:
                    cells[(row.name, label)] = ("=", "both can be on")
                elif result.success:
                    detail = "%s was switched off" % row.name
                    if result.also_changed:
                        detail += " (by pyhik: %s)" % ", ".join(sorted(result.also_changed))
                    cells[(row.name, label)] = (">", detail + (" after %s" % seen if seen else ""))
                else:
                    cells[(row.name, label)] = ("X", "refused: %s" % (
                        result.sub_status if not seen else "%s (%s)" % (result.sub_status, seen)))
        finished = True
    except ISAPIAuthError:
        stop = True  # more requests would only add failed logins
        raise
    finally:
        logger.removeHandler(codes)
        logger.setLevel(old_level)
        # The features, and what pyhik reported switching off to make room. A write that
        # raised reported nothing, so after an error everything that differs is put back.
        paths = [p for f in features for p in f.paths] + list(made_room)
        auth_error = put_back(client, report, channel, proxy, settings, before, paths, out,
                              skip=stop, anything_else=not finished)
    if auth_error:
        raise auth_error

    width = max(len(label) for _, label, _ in columns) + 2
    out("")
    out("   %-14s| turn on: %s" % ("already on", "".join("%-*s" % (width, label) for _, label, _ in columns)))
    out("   %s" % ("-" * (14 + 11 + width * len(columns))))
    for row in features:
        out("   %-14s|          %s" % (
            row.variants[0][0], "".join("%-*s" % (width, cells[(row.name, label)][0]) for _, label, _ in columns)))
    out("")
    out("   = both can be on     > the other one was switched off, the change worked")
    out("   X refused            ? could not be tried     - same setting")
    for (row_name, label), (symbol, detail) in cells.items():
        if symbol in "=>X?":
            out("   %s on, then %s: %s" % (row_name, label, detail))

    refused = [(r, c) for (r, c), (symbol, _) in cells.items() if symbol == "X"]
    tried = [v for v in cells.values() if v[0] != "-"]
    resolved = sum(1 for v in tried if v[0] == ">")
    summary = "%d cases: %d fine, %d resolved by switching the other off, %d refused" % (
        len(tried), sum(1 for v in tried if v[0] == "="), resolved, len(refused))
    for r, c in refused:
        report.add("conflict: %s on, then %s" % (r, c), "FAIL", cells[(r, c)][1])
    report.add("conflicts", "FAIL" if refused else "PASS", summary)


def check_bad_password(report, out, overrides=None):
    """A wrong password must be reported as an authentication failure."""
    out("6. wrong password, tried by each client (the device may log these attempts)")
    cfg = settings_from_env(password=os.environ["CAMERA_PASSWORD"] + "-wrong", **(overrides or {}))
    try:
        client = ISAPIClient(**cfg)
        try:
            client.get_image_channels()
        finally:
            client.close()
    except ISAPIAuthError as err:
        report.add("wrong password (ISAPIClient)",
                   "PASS" if err.status_code == 401 else "FAIL",
                   "ISAPIAuthError status=%s" % err.status_code)
    except ISAPIError as err:
        report.add("wrong password (ISAPIClient)", "FAIL", _describe(err))
    else:
        report.add("wrong password (ISAPIClient)", "FAIL", "the device accepted a wrong password")

    from pyhik.hikvision import HikCamera

    url = ("https" if cfg["ssl"] else "http") + "://" + cfg["host"]
    camera = HikCamera(url, cfg["port"], cfg["username"], cfg["password"], cfg["verify_ssl"])
    if not hasattr(camera, "auth_failed"):
        report.add("wrong password (HikCamera.auth_failed)", "SKIP",
                   "this pyhik has no auth_failed (pyHik PR 6)")
    elif camera.get_id is None and camera.auth_failed:
        report.add("wrong password (HikCamera.auth_failed)", "PASS")
    else:
        report.add("wrong password (HikCamera.auth_failed)", "FAIL",
                   "device id=%s auth_failed=%s" % (camera.get_id, camera.auth_failed))


def detect_proxy(client, proxy, out):
    """Decide whether to use the proxy endpoints (unless --proxy/--no-proxy says).

    An NVR or DVR is recognised by its device type. That is not always enough: some
    recorders report the type of a camera (IPC, for example), some do not answer the
    device information request at all, and real cameras answer both. So unless the
    type says NVR or DVR, the camera endpoints are tried first, and if they fail with
    a device error the proxy endpoints are tried before giving up.

    An authentication or connection error on an image endpoint stops the detection.
    """
    if proxy is not None:
        out("proxy endpoints: %s (set on the command line)" % ("yes" if proxy else "no"))
        return proxy
    try:
        device_type = str(client.get_device_type()).strip()
    except ISAPIAuthError:
        raise
    except Exception as err:  # the endpoints below can still tell
        device_type = None
        out("device type: unknown (could not read it: %s)" % type(err).__name__)
    if (device_type or "").upper() in ("NVR", "DVR"):
        out("proxy endpoints: yes (device type %s)" % device_type)
        return True
    try:
        client.get_image_channels(proxy=False)
    except (ISAPIAuthError, ISAPIConnectionError):
        raise
    except ISAPIError as err:
        out("camera image endpoints failed (%s); trying the proxy endpoints" % _describe(err))
        try:
            if client.get_image_channels(proxy=True):
                out("proxy endpoints: yes (the proxy endpoints work)")
                return True
        except (ISAPIAuthError, ISAPIConnectionError):
            raise
        except ISAPIError:
            pass
    out("proxy endpoints: no (device type %s)" % (device_type or "unknown"))
    return False


def run(client, channel=None, proxy=None, write=False, path=None, conflicts=False,
        bad_password=False, out=print, report=None, overrides=None):
    """Return 0 if every check passed, 1 otherwise.

    overrides: host, port and username given on the command line, for --bad-password.
    """
    report = report or Report(out)
    _device_line(client, out)
    proxy = detect_proxy(client, proxy, out)
    found = check_read(client, report, channel, proxy, out)
    if found is not None:
        channel, settings = found
        if write:
            check_write(client, report, channel, proxy, settings, path, out)
        if conflicts:
            check_conflicts(client, report, channel, proxy, settings, out)
    if bad_password:
        check_bad_password(report, out, overrides)
    if not (write or conflicts or bad_password):
        out("read-only checks done; see --help for the checks that change a value")
    report.summary()
    return 1 if report.failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pyhik checkisapi", description=__doc__.split("\n")[0])
    parser.add_argument("--host", help="host name or IP address (default: CAMERA_HOST)")
    parser.add_argument("--port", type=int, help="port (default: CAMERA_PORT, else 80)")
    parser.add_argument("--username", help="user name (default: CAMERA_USERNAME, else admin)")
    parser.add_argument("--ssl", action="store_true",
                        help="use HTTPS, on port 443 unless --port says otherwise (default: CAMERA_SSL)")
    parser.add_argument("--no-verify", action="store_true",
                        help="do not check the device's certificate, for a self-signed one (default: CAMERA_VERIFY_SSL=0)")
    parser.add_argument("--proxy", dest="proxy", action="store_true", default=None,
                        help="force the NVR endpoints (normally detected)")
    parser.add_argument("--no-proxy", dest="proxy", action="store_false",
                        help="force the camera endpoints (normally detected)")
    parser.add_argument("--channel", type=int, help="channel ID (default: first listed)")
    parser.add_argument("--write", action="store_true", help="change one setting and restore it (a number by one step; with --path also a switch or a select)")
    parser.add_argument("--path", help="setting to change with --write: a number, switch or select (default Color/brightnessLevel)")
    parser.add_argument("--conflicts", action="store_true",
                        help="try every WDR / HLC / BLC / defog combination and print a table (restores them)")
    parser.add_argument("--bad-password", action="store_true",
                        help="try a wrong password through each client (at least two failed logins)")
    parser.add_argument("--all", action="store_true", help="--write --conflicts --bad-password")
    parser.add_argument("-v", "--verbose", action="store_true", help="show pyhik's debug log")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="      [%(name)s] %(message)s")
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    overrides = dict(host=args.host, port=args.port, username=args.username,
                     ssl=True if args.ssl else None, verify_ssl=False if args.no_verify else None)
    client = client_from_env(**overrides)
    try:
        return run(client, args.channel, args.proxy, args.write or args.all, args.path,
                   args.conflicts or args.all, args.bad_password or args.all,
                   overrides=overrides)
    except ISAPIAuthError as err:
        print("authentication/permission problem, stopping: " + _describe(err))
    except ISAPINotSupportedError as err:
        print("not supported by this device: " + _describe(err))
    except (ISAPIConnectionError, ISAPIError) as err:
        print("request failed: " + _describe(err))
    return 2


if __name__ == "__main__":
    sys.exit(main())
