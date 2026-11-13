# Testing the image settings on a real camera or NVR

The unit tests (`python -m unittest discover test -v`) never touch a device. This page is
for trying the code on real hardware before it is released and for reporting what
happens. `python -m pyhik checkisapi` does the work.

## Before you start

- Use an account that has remote configuration rights. The script never prints the
  password and stops at the first authentication error.
- Everything that changes a value puts it back afterwards, even after an error. If it
  could not, it says `NOT RESTORED` and tells you the value to set by hand.
- Try a camera you do not mind experimenting. The changes are small (one brightness
  step, WDR and HLC toggled, or the one setting you pick with `--path`) but they are
  real writes.

```bash
git clone git@github.com:mezz64/pyHik.git
cd pyHik
python -m venv .venv && . .venv/bin/activate && pip install -e .
```

## Required Environment Variables

```bash
export CAMERA_HOST=192.168.1.100 # IP address or hostname of your camera or NVR.
export CAMERA_USERNAME=admin
export CAMERA_PASSWORD='passwd123'
```
> For security, the password is read exclusively from the environment.

## Optional Environment Variables

```bash
export CAMERA_PORT=80      # Custom port (default: 80; 443 enables HTTPS)
export CAMERA_SSL=1        # Enable SSL/HTTPS
export CAMERA_VERIFY_SSL=0 # Disable SSL cert verification
```

You may also use arguments:

* `--host IP/hostname` (`CAMERA_HOST=...`)
* `--username admin `  (`CAMERA_USERNAME=...`)
* `--port 443`         (`CAMERA_PORT=...`)
* `--ssl`              (`CAMERA_SSL=1`)
* `--no-verify`        (`CAMERA_VERIFY_SSL=0`)

## What to run

| Command | What it checks | Changes the device? |
|---|---|---|
| `python -m pyhik checkisapi` | channels, settings (with options and ranges), current values | No |
| `python -m pyhik checkisapi --write` | changes one number by one step (brightness), reads it back, restores it. With `--path` it can change a switch or a select instead (see below). If the change switched another setting off to make room, that one is put back too | Briefly |
| `python -m pyhik checkisapi --conflicts` | WDR, HLC, BLC and defog cannot all be on together. For every pair that the device offers it turns one on, then the other and prints a table of what happened. Checks that pyhik switches the other one off by itself and shows the refusal code the device used | Briefly (restores everything) |
| `python -m pyhik checkisapi --bad-password` | a wrong password is reported as an authentication failure by each client: `ISAPIAuthError` 401 from `ISAPIClient` and `HikCamera.auth_failed` | No but it makes at least two failed logins and the device may log them. Do not use it on an account close to being locked |
| `python -m pyhik checkisapi --all` | all of the above | Briefly |
| add `--proxy` / `--no-proxy` | force the NVR (ImageProxy) or the camera endpoints. Normally not needed: the script uses the NVR endpoints for an NVR or DVR, and for any device whose camera image endpoints fail while the NVR endpoints work | as above |
| add `--channel N` | use channel ID N instead of the first listed | as above |
| add `--path Some/path` | with `--write`, change this setting instead of brightness. Take the path from the list the read-only run prints (a plain `--write` also suggests a few switch and select paths). A number moves one step, a switch flips (true to false or false to true), a select takes the first option that is not the current one. Always put back afterwards | as above |
| add `-v` | print pyhik's debug log, which shows the retries and conflict handling | |

Each check ends with `PASS`, `FAIL` or `SKIP` and the run ends with a summary. The exit
code is 0 when nothing failed, 1 when a check failed and 2 when the device could not be
reached or refused the login.

A `SKIP` is normal: for example `--conflicts` is skipped on a device that offers fewer than
two of WDR, HLC, BLC and defog, and `--write --path` is skipped when the path is not in the
list or the setting has no other value to try.

### Changing a switch or a select

```bash
python -m pyhik checkisapi --channel 1 --write --path enableImageLossDetection
```

The script prints what it changes (`path (switch): true -> false`), the result, the value
read back, and `restore ...: restored`. Use the exact path from the read-only run: an NVR
can list settings with other names than a camera does.

### What the `--conflicts` table looks like

```
   already on    | turn on: WDR=open    WDR=auto    HLC         BLC=CENTER
   -------------------------------------------------------------------------
   WDR=open      |          -           -           >           >
   HLC           |          >           >           -           =
   BLC=CENTER    |          >           >           =           -

   = both can be on     > the other one was switched off, the change worked
   X refused            ? could not be tried     - same setting
   WDR on, then HLC: WDR was switched off (by pyhik: WDR/mode) after WDROrDehazeMutexHLC
   HLC on, then WDR=open: HLC was switched off (by pyhik: HLC/enabled) after HLCMutexWDROrDehaze
   ...
```

Read it as "with the row on, turn the column on". `>` is the normal result on a camera that
has the conflict: the device refused with the code shown, pyhik switched the other setting off
and the change went through. `=` means this device allows both. `X` is a failure: the device
refused and pyhik did not know how to resolve it. That is the case to report, with the code
shown. The cases depend on which of WDR, HLC, BLC and defog your device offers; with fewer
than two the check is skipped. The camera's original values are put back at the end
(`restore: restored`).

## NVRs

Nothing special is needed: the script asks the device for its type and, for an NVR or DVR (or a device
whose camera endpoints fail while the NVR endpoints work), lists the attached cameras as channels and reads and writes them through the ImageProxy
endpoints. It prints `proxy endpoints: yes ...` with the reason, so you can see what it chose.
`--write` and `--conflicts` act on the first channel, or on the one given with `--channel`,
so you can test one camera behind the NVR at a time. Run the read-only check first.
If you report an issue, include if the NVR is a Hikvision or a re-branded one.

### When the device information cannot be read

If the device information request fails, the script prints, for example,
`device: model=? (ISAPIError: Request failed with status 503), ...` and carries on: it asks
only once, and falls back to trying the camera endpoints and then the NVR endpoints. That line
is not a failure of the check. Please include it in a report, because it has the error.

A rejected login or a missing permission (401 or 403) is not carried on from: the run
stops at that request, here as everywhere else; a wrong password is not tried again.

## Reporting a problem

Open an issue (or paste into the pull request) with:

1. The full output of the run, with `-v` for anything that failed. The script already
   prints the pyhik version and the model, type and firmware of the device. Remove the
   host name, IP addresses and anything else that identifies you.
2. Whether it is a camera or an NVR and whether it is rebranded.
3. What you expected and what happened, including whether the setting really changed
   on the device (its own web page shows the truth).
4. The exact command you ran.

Do not include the password, serial number or MAC address.
