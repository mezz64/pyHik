# Introduction

This is a python module aiming to expose common API events from a Hikvision IP camera.

Code is licensed under the MIT license.


# Requirements

If internal callback methods are used no external libraries are required, otherwise:
* [pyDispatcher] 2.0.5 

# Installation

```pip install pyhik```

# Usage

```python
import pyhik.hikvision

camera = pyhik.hikvision.HikCamera('http://X.X.X.X', port=80, user='admin', pass='1234')
```

# Available Methods

* Callbacks
event_callback - provides camera id and event type that changed.
data_callback - provides the event_state list on an event change.

* Properties
get_id - returns unique camera id
get_name - returns camera name
current_event_states - returns the event state list

* Functions
start_stream - initialzes the event stream processing thread
disconnect - closes the http stream session and stops the processing thread

# TODO

* Support motion detection status and ability to turn on/off
* Support IR day/night status and ability to switch between day/night/auto
