#!/usr/bin/env python3
"""Check pyhik's image settings support against a real camera or NVR.

Not a unit test: unit tests must run without hardware.

Run it from the root of a pyHik checkout that has the image settings code:

    export CAMERA_HOST=192.168.1.100
    export CAMERA_USERNAME=admin
    export CAMERA_PASSWORD=password123
    PYTHONPATH=. python tools/live_image_check.py              # read-only, always safe

(`PYTHONPATH=.` makes the pyhik/ folder of the checkout the one imported. It's not needed
if you run `pip install -e .` from the repo root and the prefix `tools` is left out below.)

What to run:

    python tools/live_image_check.py                  # read-only, lists every setting and its path
    python tools/live_image_check.py --write          # change Color/brightnessLevel by one step and put it back
    python tools/live_image_check.py --conflicts      # WDR / HLC / BLC / defog conflict matrix
    python tools/live_image_check.py --bad-password   # one login with a wrong password
    python tools/live_image_check.py --all            # all of the above
    python tools/live_image_check.py -v               # also show pyhik's debug log (the retries)

--write changes the setting Color/brightnessLevel by one step (or the first numeric
setting if the device has no brightness) and restores it. To choose the setting yourself
(it must be a numeric one), take a path from the list printed by the read-only run.
--path is only used together with --write:

    python tools/live_image_check.py --write --path Color/contrastLevel
    python tools/live_image_check.py --write --path Sharpness/SharpnessLevel

One channel is checked per run. Without --channel, the first listed channel is used. On an NVR,
give the channel ID of the attached camera, for example the camera on channel 3:

    python tools/live_image_check.py --channel 3
    python tools/live_image_check.py --channel 3 --write --path Color/contrastLevel

An NVR is detected from the device's own type (NVR or DVR). A recorder that reports the
type of a camera (IPC) is detected by its camera image endpoints failing while the proxy
endpoints work. Its attached cameras are then used through the proxy endpoints.
--proxy and --no-proxy override the detection.

Optional: CAMERA_PORT (80), CAMERA_SSL=1, CAMERA_VERIFY_SSL=0.

The password is read from the environment and is never printed.

Everything that changes a value puts it back afterwards (even after an error), and
the script says so when it could not. It stops at the first authentication error,
because repeated failures can lock the account. --bad-password makes exactly one
login attempt with a wrong password (two requests: one per client class); do not
use it on an account that is close to being locked.
"""

import argparse
import logging
import os
import re
import sys

try:
    import pyhik
    from pyhik.image import ImageSettingType
    from pyhik.isapi import (
        ISAPIAuthError,
        ISAPIClient,
        ISAPIConnectionError,
        ISAPIError,
        ISAPINotSupportedError,
    )
except ImportError as _err:  # an older pyhik without the image settings code
    import pyhik

    sys.exit(
        "This pyhik has no image settings support (%s).\n"
        "  pyhik loaded from: %s\n"
        "Check out a pyHik branch that has the image settings code and run this\n"
        "script from the repo root with PYTHONPATH=. so that its pyhik/ folder is the\n"
        "one imported. If pyhik was installed with pip, run `pip install -e .` in the\n"
        "repo on that branch (or `pip uninstall pyhik`)."
        % (_err, getattr(pyhik, "__file__", "unknown"))
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


def settings_from_env(password=None):
    missing = [n for n in ("CAMERA_HOST", "CAMERA_PASSWORD") if not os.environ.get(n)]
    if missing:
        sys.exit("Set these environment variables first: " + ", ".join(missing)
                 + " (optional: CAMERA_USERNAME, CAMERA_PORT, CAMERA_SSL, CAMERA_VERIFY_SSL)")
    return dict(
        host=os.environ["CAMERA_HOST"],
        port=int(os.environ.get("CAMERA_PORT", "80")),
        username=os.environ.get("CAMERA_USERNAME", "admin"),
        password=password if password is not None else os.environ["CAMERA_PASSWORD"],
        ssl=_flag("CAMERA_SSL", False),
        verify_ssl=_flag("CAMERA_VERIFY_SSL", True),
    )


def client_from_env(password=None):
    return ISAPIClient(**settings_from_env(password))


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


def _device_line(client, out):
    """Print what a bug report needs to know about the device (no serial number)."""
    parts = []
    for label, getter in (
        ("model", "get_device_model"),
        ("type", "get_device_type"),
        ("firmware", "get_firmware_version"),
    ):
        try:
            parts.append("%s=%s" % (label, getattr(client, getter)() or "?"))
        except Exception as err:  # not worth failing the run for
            parts.append("%s=? (%s)" % (label, type(err).__name__))
    out("pyhik %s from %s" % (pyhik.__version__, os.path.dirname(pyhik.__file__)))
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


def check_write(client, report, channel, proxy, settings, path, out):
    """Change one numeric setting, read it back, put it back."""
    out("4. change a numeric setting and put it back")
    target = pick_number(settings, path)
    if target is None or target.value is None:
        report.add("write", "SKIP", "no suitable numeric setting (use --path)")
        return
    original = target.value
    high = int(target.max_value)
    new = str(int(original) + 1 if int(original) < high else int(original) - 1)
    out("   %s: %s -> %s" % (target.path, original, new))
    try:
        result = client.set_image_setting(target.path, new, channel, proxy=proxy)
        out("   result: %s" % result)
        got = client.get_image_values(channel, proxy=proxy).get(target.path)
        ok = result.success and got == new
        report.add("write", "PASS" if ok else "FAIL",
                   "read back OK" if ok else "read back %s" % got)
    finally:
        result = client.set_image_setting(target.path, original, channel, proxy=proxy)
        got = client.get_image_values(channel, proxy=proxy).get(target.path)
        restored = result.success and got == original
        out("   restore %s -> %s: %s" % (
            target.path, original, "restored" if restored else "NOT RESTORED, set it back by hand"))
        if not restored:
            report.add("restore of " + target.path, "FAIL", "set it back to %s by hand" % original)


class Feature:
    """A setting that can be on or off and may exclude others (WDR, HLC, BLC, defog)."""

    def __init__(self, name, variants, off, is_on, restore_on, paths):
        self.name = name
        self.variants = variants  # [(label, apply)] ways to turn it on; apply() -> result
        self.off = off  # () -> result
        self.is_on = is_on  # (values) -> bool
        self.restore_on = restore_on  # (values before) -> result, puts the old "on" state back
        self.paths = paths


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
            lambda v, p=setting.path: put(p, v[p])(),
            [setting.path],
        ))

    mode_feature("WDR", "wdr/mode")
    mode_feature("defog", "dehaze/dehazemode")

    hlc = by_path.get("hlc/enabled")
    if hlc is not None:
        features.append(Feature(
            "HLC", [("HLC", put(hlc.path, "true"))], put(hlc.path, "false"),
            lambda v, p=hlc.path: v.get(p) == "true",
            lambda v, p=hlc.path: put(p, "true")(), [hlc.path],
        ))

    blc = by_path.get("blc/blcmode")
    if blc is not None and blc.linked_enabled_path and blc.off_value is not None:
        modes = [o for o in blc.options if o != blc.off_value]
        preferred = next((o for o in modes if o.lower() in ("auto", "center")), None)
        mode = preferred or (modes[0] if modes else None)
        if mode is not None:
            def blc_set(value, blc=blc):
                return client.set_image_linked_setting(blc, value, channel, proxy=proxy)

            features.append(Feature(
                "BLC", [("BLC=%s" % mode, lambda: blc_set(mode))], lambda: blc_set(blc.off_value),
                lambda v, e=blc.linked_enabled_path: v.get(e) == "true",
                lambda v, b=blc: blc_set(v.get(b.path) or mode),
                [blc.path, blc.linked_enabled_path],
            ))
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
    try:
        for row in features:
            for feature, label, apply in columns:
                if feature is row:
                    cells[(row.name, label)] = ("-", "")
                    continue
                for f in features:  # known starting point: everything off
                    f.off()
                first = row.variants[0]
                setup = first[1]()
                if not setup.success:
                    cells[(row.name, label)] = ("?", "could not turn %s on first: %s" % (
                        first[0], setup.error_description))
                    continue
                codes.codes.clear()
                result = apply()
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
    finally:
        logger.removeHandler(codes)
        logger.setLevel(old_level)
        for f in features:
            f.off()
        for f in features:
            if f.is_on(before):
                f.restore_on(before)
        after = read()
        restored = all(after.get(p) == before.get(p) for f in features for p in f.paths)
        out("   restore: %s" % ("restored" if restored else "NOT RESTORED, put these back by hand: " + ", ".join(
            "%s=%s" % (p, before.get(p)) for f in features for p in f.paths if after.get(p) != before.get(p))))
        if not restored:
            report.add("restore of conflict settings", "FAIL", "see above")

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


def check_bad_password(report, out):
    """A wrong password must be reported as an authentication failure."""
    out("6. one login with a wrong password (the device may log this attempt)")
    cfg = settings_from_env(password=os.environ["CAMERA_PASSWORD"] + "-wrong")
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

    An NVR or DVR is recognised by its device type. Some recorders report the type of a
    camera (IPC, for example) and real cameras report it too; the type alone is not
    enough: if the camera endpoints fail, the proxy endpoints are tried before giving up.
    """
    if proxy is not None:
        out("proxy endpoints: %s (set on the command line)" % ("yes" if proxy else "no"))
        return proxy
    try:
        device_type = client.get_device_type()
    except Exception as err:  # fall back to a camera
        out("proxy endpoints: no (could not read the device type: %s)" % type(err).__name__)
        return False
    if str(device_type).strip().upper() in ("NVR", "DVR"):
        out("proxy endpoints: yes (device type %s)" % device_type)
        return True
    try:
        client.get_image_channels(proxy=False)
    except (ISAPIAuthError, ISAPIConnectionError):
        pass  # not a reason to try other endpoints; the checks report it
    except ISAPIError as err:
        out("camera image endpoints failed (%s); trying the proxy endpoints" % _describe(err))
        try:
            if client.get_image_channels(proxy=True):
                out("proxy endpoints: yes (device type %s, but the proxy endpoints work)" % device_type)
                return True
        except ISAPIError:
            pass
    out("proxy endpoints: no (device type %s)" % device_type)
    return False


def run(client, channel=None, proxy=None, write=False, path=None, conflicts=False,
        bad_password=False, out=print, report=None):
    """Return 0 if every check passed, 1 otherwise."""
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
        check_bad_password(report, out)
    if not (write or conflicts or bad_password):
        out("read-only checks done; see --help for the checks that change a value")
    report.summary()
    return 1 if report.failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--proxy", dest="proxy", action="store_true", default=None,
                        help="force the NVR endpoints (normally detected)")
    parser.add_argument("--no-proxy", dest="proxy", action="store_false",
                        help="force the camera endpoints (normally detected)")
    parser.add_argument("--channel", type=int, help="channel ID (default: first listed)")
    parser.add_argument("--write", action="store_true", help="change one numeric setting by one step and restore it")
    parser.add_argument("--path", help="numeric setting to change with --write (default Color/brightnessLevel)")
    parser.add_argument("--conflicts", action="store_true",
                        help="try every WDR / HLC / BLC / defog combination and print a table (restores them)")
    parser.add_argument("--bad-password", action="store_true",
                        help="make one login with a wrong password")
    parser.add_argument("--all", action="store_true", help="--write --conflicts --bad-password")
    parser.add_argument("-v", "--verbose", action="store_true", help="show pyhik's debug log")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="      [%(name)s] %(message)s")
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    client = client_from_env()
    try:
        return run(client, args.channel, args.proxy, args.write or args.all, args.path,
                   args.conflicts or args.all, args.bad_password or args.all)
    except ISAPIAuthError as err:
        print("authentication/permission problem, stopping: " + _describe(err))
    except ISAPINotSupportedError as err:
        print("not supported by this device: " + _describe(err))
    except (ISAPIConnectionError, ISAPIError) as err:
        print("request failed: " + _describe(err))
    return 2


if __name__ == "__main__":
    sys.exit(main())
