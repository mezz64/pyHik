# Introduction

This is a python module aiming to expose common API events from a Hikvision IP camera.

Code is licensed under the MIT license.

Getting Started
===============

Intro.


# Requirements

* [pyDispatcher] 2.0.5 

# Installation

```pip install pyhik```

# Usage

```python
import pyhik.hikvision

camera = pyhik.hikvision.HikCamera('http://X.X.X.X', port=80, user='admin', pass='1234')
```

# Available Methods

xxxx

# TODO

* Support motion detection status and ability to turn on/off
* Support IR day/night status and ability to switch between day/night/auto
