"""
Sample program for hikvision api.
"""

import logging
import sys
# setting path
sys.path.append('../')

import pyhik.hikvision as hikvision

logging.basicConfig(filename='out.log', filemode='w', level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')


class HikCamObject(object):
    """Representation of HIk camera."""

    def __init__(self, url, port, user, passw):
        """initalize camera"""

        # Establish camera
        self.cam = hikvision.HikCamera(url, port, user, passw)

        self._name = self.cam.get_name
        self.motion = self.cam.current_motion_detection_state

        # Start event stream
        self.cam.start_stream()

        self._event_states = self.cam.current_event_states
        self._id = self.cam.get_id

        print('NAME: {}'.format(self._name))
        print('ID: {}'.format(self._id))
        print('{}'.format(self._event_states))
        print('Motion Detect State: {}'.format(self.motion))

    @property
    def sensors(self):
        """Return list of available sensors and their states."""
        return self.cam.current_event_states

    def get_attributes(self, sensor, channel, region):
        """Return attribute list for sensor/channel."""
        return self.cam.fetch_attributes(sensor, channel, region)

    def stop_hik(self):
        """Shutdown Hikvision subscriptions and subscription thread on exit."""
        self.cam.disconnect()

    def flip_motion(self, value):
        """Toggle motion detection"""
        if value:
            self.cam.enable_motion_detection()
        else:
            self.cam.disable_motion_detection()


class HikSensor(object):
    """ Hik camera sensor."""

    def __init__(self, sensor, channel, cam, region):
        """Init"""
        self._cam = cam
        self._name = "{} {} {} {}".format(self._cam.cam.name, sensor, channel, region)
        self._id = "{}.{}.{}.{}".format(self._cam.cam.cam_id, sensor, channel, region)
        self._region = region
        self._sensor = sensor
        self._channel = channel

        self._cam.cam.add_update_callback(self.update_callback, self._id)

    def _sensor_state(self):
        """Extract sensor state."""
        return self._cam.get_attributes(self._sensor, self._channel, self._region)[0]

    def _sensor_last_update(self):
        """Extract sensor last update time."""
        return self._cam.get_attributes(self._sensor, self._channel, self._region)[3]

    @property
    def name(self):
        """Return the name of the Hikvision sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return an unique ID."""
        return '{}.{}'.format(self.__class__, self._id)

    @property
    def is_on(self):
        """Return true if sensor is on."""
        return self._sensor_state()

    def update_callback(self, msg):
        """ get updates. """
        print('Callback: {}'.format(msg))
        print('{}:{} @ {}'.format(self.name, self._sensor_state(), self._sensor_last_update()))


def main():
    """Main function"""
    cam = HikCamObject('http://XXX.XXX.XXX.XXX', 80, 'user', 'password')

    entities = []

    for sensor, channel_list in cam.sensors.items():
        for channel in channel_list:
            entities.append(HikSensor(sensor, channel[1], cam, channel[4]))

main()
