"""Constants for LK Arc Climate."""

DOMAIN = "lk_arc_climate"
LK_DOMAIN = "lksystems"

# Official ha-lksystems only creates climate for deviceRole == "arc-tune".
# Many Arc Sense thermostats (controllable in the LK app) never get that role
# in the cloud payload — they still expose desiredTemperature and accept the
# same set-temperature API. This integration creates climate for all of them.
MIN_TEMP = 5.0
MAX_TEMP = 30.0
TEMP_STEP = 0.5
