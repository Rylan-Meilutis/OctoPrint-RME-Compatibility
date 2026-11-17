"""Import the actual serial core without 2.0's deprecated forwarding module."""
try:
    from octoprint.plugins.serial_connector import serial_comm as core
    from octoprint.plugins.serial_connector.connector import ConnectedSerialPrinter
except ImportError:
    from octoprint.util import comm as core
    ConnectedSerialPrinter = None

MachineCom = core.MachineCom
QueueMarker = core.QueueMarker
SendQueueMarker = core.SendQueueMarker
PrintingGcodeFileInformation = core.PrintingGcodeFileInformation
