[![PyPI](https://img.shields.io/pypi/v/pyHik.svg)](https://pypi.python.org/pypi/pyHik)

# Introduction

This is a python module aiming to expose common API events from a Hikvision IP camera or nvr.  Most rebadged models work as well with full functionality.

Code is licensed under the MIT license.


# Requirements

If internal callback methods are used no external libraries are required, otherwise:
* [pyDispatcher] 2.0.5 

# Installation

```pip install pyhik```

# Usage

```python
import pyhik.hikvision

camera = pyhik.hikvision.HikCamera('http://X.X.X.X', port=80, usr='admin', pwd='1234')
```

# Available Methods

### Callbacks
* add_update_callback(callback, msg) - used to register an update callback function.
** msg should take the form: cam_id.event_type.channel

### Properties
* get_id - returns unique camera/nvr id
* get_name - returns camera/nvr name
* current_event_states - returns the event state dictionary

### Functions
* start_stream - initialzes the event stream processing thread
* disconnect - closes the http stream session and stops the processing thread

# TODO

* Support motion detection status and ability to turn on/off
* Support IR day/night status and ability to switch between day/night/auto
