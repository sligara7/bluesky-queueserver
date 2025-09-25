"""
Device Coordination Service Package

This package provides device coordination and lock management functionality
for the Bluesky Queue Server. It mediates device access between
the Queue Server and other services (like Ophyd WebSocket) to prevent
conflicts and ensure safe operations.

Key Features:
- Device lock management with priorities
- Real-time conflict detection
- Inter-service communication
- Lock timeout handling
- WebSocket and HTTP API
- Integration with shared device configuration
"""

from .service import DeviceCoordinationService

__all__ = [
    'DeviceCoordinationService'
]
