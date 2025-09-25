"""
Device Configuration API

This module provides REST API endpoints for accessing shared device configurations.
These endpoints can be used by both Queue Server and external integrations
to get consistent device information.

Note: This module requires FastAPI and Pydantic to be installed for full functionality.
"""

from typing import Dict, List, Optional, Any
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .device_config import DeviceExportUtilities

logger = logging.getLogger(__name__)


class DeviceInfo(BaseModel):
    """Device information response model."""
    name: str
    device_class: str
    device_type: str
    capabilities: Dict[str, bool]
    module: str


class DeviceListResponse(BaseModel):
    """Response model for device list endpoints."""
    success: bool
    devices: Dict[str, DeviceInfo]
    total_count: int
    device_type_filter: Optional[str] = None


class DeviceConfigResponse(BaseModel):
    """Response model for device configuration endpoints."""
    success: bool
    config_format: str  # 'queueserver' or 'device_config'
    config: Dict[str, Any]
    device_count: int


router = APIRouter(prefix="/api/device-config", tags=["device-config"])

# Global device utilities instance
_device_utils: Optional[DeviceExportUtilities] = None


def initialize_device_api(profile_collection_path: str):
    """Initialize the global device export utilities."""
    global _device_utils
    _device_utils = DeviceExportUtilities()
    
    # Load devices from profile collection
    try:
        _device_utils.load_devices_from_profile(profile_collection_path)
        logger.info(f"Device API initialized with {len(_device_utils.get_device_names())} devices")
    except Exception as e:
        logger.error(f"Failed to initialize device API: {e}")
        _device_utils = None


def get_device_utils() -> DeviceExportUtilities:
    """Get the global device export utilities."""
    if _device_utils is None:
        raise HTTPException(
            status_code=500, 
            detail="Device utilities not initialized"
        )
    return _device_utils


@router.get("/devices", response_model=DeviceListResponse)
async def list_devices(
    device_type: Optional[str] = Query(None, description="Filter devices by type (motor, detector, signal, etc.)")
):
    """
    List all available devices with optional filtering.
    
    Parameters
    ----------
    device_type : str, optional
        Filter devices by type
        
    Returns
    -------
    DeviceListResponse
        List of devices matching the criteria
    """
    utils = get_device_utils()
    
    # Get all devices
    if device_type:
        device_defs = utils.get_devices_by_type(device_type)
    else:
        device_defs = {name: utils.get_device(name) for name in utils.get_device_names()}
    
    # Convert to response format
    devices = {
        name: DeviceInfo(
            name=dev.name,
            device_class=dev.device_class,
            device_type=dev.device_type,
            capabilities=dev.capabilities,
            module=dev.module
        )
        for name, dev in device_defs.items()
        if dev is not None
    }
    
    return DeviceListResponse(
        success=True,
        devices=devices,
        total_count=len(devices),
        device_type_filter=device_type
    )


@router.get("/devices/{device_name}", response_model=DeviceInfo)
async def get_device(device_name: str):
    """
    Get detailed information about a specific device.
    
    Parameters
    ----------
    device_name : str
        Name of the device
        
    Returns
    -------
    DeviceInfo
        Device information
    """
    utils = get_device_utils()
    device_def = utils.get_device(device_name)
    
    if device_def is None:
        raise HTTPException(
            status_code=404, 
            detail=f"Device '{device_name}' not found"
        )
    
    return DeviceInfo(
        name=device_def.name,
        device_class=device_def.device_class,
        device_type=device_def.device_type,
        capabilities=device_def.capabilities,
        module=device_def.module
    )


@router.get("/config/queueserver", response_model=DeviceConfigResponse)
async def get_queueserver_config():
    """
    Get device configuration in Queue Server format.
    This uses bluesky-queueserver's existing functionality.
    
    Returns
    -------
    DeviceConfigResponse
        Device configuration for Queue Server
    """
    # Note: This would need to use bluesky-queueserver's existing export functionality
    # For now, return a placeholder response
    return DeviceConfigResponse(
        success=False,
        config_format="queueserver",
        config={"message": "Use bluesky-queueserver's existing export functionality"},
        device_count=0
    )


@router.get("/config/device", response_model=DeviceConfigResponse)
async def get_device_config():
    """
    Get device configuration in JSON format for external integrations.
    
    Returns
    -------
    DeviceConfigResponse
        Device configuration for external systems
    """
    utils = get_device_utils()
    config = utils.export_device_config()
    
    return DeviceConfigResponse(
        success=True,
        config_format="device_config",
        config=config,
        device_count=len(utils.get_device_names())
    )


@router.post("/reload")
async def reload_device_config(profile_collection_path: Optional[str] = None):
    """
    Reload device configurations from the profile collection.
    
    This endpoint allows dynamic reloading of device configurations
    without restarting the service.
    
    Parameters
    ----------
    profile_collection_path : str, optional
        Path to reload from. If not provided, uses the current path.
    
    Returns
    -------
    dict
        Status of the reload operation
    """
    global _device_utils
    
    try:
        if profile_collection_path is None and _device_utils is None:
            raise HTTPException(
                status_code=400,
                detail="No profile collection path provided and none previously configured"
            )
        
        # Create new utilities instance
        new_utils = DeviceExportUtilities()
        
        # Use provided path or re-use existing logic to determine path
        if profile_collection_path:
            device_defs = new_utils.load_devices_from_profile(profile_collection_path)
        else:
            # This would need to be implemented based on how you want to handle reloading
            raise HTTPException(
                status_code=400,
                detail="Profile collection path required for reload"
            )
        
        # Update global instance
        _device_utils = new_utils
        
        return {
            "success": True,
            "message": "Device configurations reloaded successfully",
            "device_count": len(device_defs),
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reload device configurations: {str(e)}"
        )


@router.get("/status")
async def get_status():
    """
    Get the status of the device configuration service.
    
    Returns
    -------
    dict
        Service status information
    """
    utils = get_device_utils()
    device_names = utils.get_device_names()
    device_types = []
    
    for name in device_names:
        device = utils.get_device(name)
        if device:
            device_types.append(device.device_type)
    
    return {
        "success": True,
        "service": "device-configuration-api",
        "device_count": len(device_names),
        "device_types": list(set(device_types)),
        "endpoints": [
            "/devices",
            "/devices/{device_name}",
            "/config/queueserver",
            "/config/device",
            "/reload",
            "/status"
        ]
    }
