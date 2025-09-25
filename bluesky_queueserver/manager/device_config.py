"""
Device Configuration Utilities - OaaS Integration Support

This module provides additional device configuration export formats 
and utilities that complement bluesky-queueserver's existing capabilities.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, asdict

# Use bluesky-queueserver's existing functionality
from .profile_ops import (
    load_profile_collection,
    existing_plans_and_devices_from_nspace,
    save_existing_plans_and_devices
)

logger = logging.getLogger(__name__)


@dataclass
class DeviceDefinition:
    """Standard device definition structure for external integrations."""
    name: str
    device_class: str
    device_type: str
    module: str
    capabilities: Dict[str, bool]  # flyable, movable, readable
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DeviceDefinition':
        """Create from dictionary."""
        return cls(**data)


class DeviceExportUtilities:
    """
    Device export utilities for external integrations (e.g., OaaS).
    Uses bluesky-queueserver's existing device discovery capabilities.
    """
    
    def __init__(self):
        """Initialize Device Export Utilities."""
        self._device_definitions: Dict[str, DeviceDefinition] = {}
        
    def load_devices_from_profile(self, profile_collection_dir: Union[str, Path]) -> Dict[str, DeviceDefinition]:
        """
        Load device definitions using bluesky-queueserver's existing functionality.
        
        Parameters
        ----------
        profile_collection_dir : str or Path
            Path to the profile collection directory (contains startup/ subdirectory)
            
        Returns
        -------
        dict
            Dictionary of device name -> DeviceDefinition
        """
        profile_path = Path(profile_collection_dir)
        if not profile_path.exists():
            raise FileNotFoundError(f"Profile collection directory not found: {profile_path}")
            
        logger.info(f"Loading devices using queue server discovery from: {profile_path}")
        
        # Use bluesky-queueserver's existing device discovery
        startup_path = profile_path / "startup" if (profile_path / "startup").exists() else profile_path
        namespace = load_profile_collection(startup_path, patch_profiles=True, keep_re=False)
        existing_plans, existing_devices, plans_in_nspace, devices_in_nspace = existing_plans_and_devices_from_nspace(
            nspace=namespace, max_depth=0
        )
        
        # Convert to DeviceDefinition format
        self._device_definitions.clear()
        for name, device_info in existing_devices.items():
            device_def = self._create_device_definition_from_existing(name, device_info)
            if device_def:
                self._device_definitions[name] = device_def
        
        logger.info(f"Loaded {len(self._device_definitions)} devices")
        return self._device_definitions.copy()
    
    def _create_device_definition_from_existing(self, name: str, device_info: Dict[str, Any]) -> Optional[DeviceDefinition]:
        """Create DeviceDefinition from existing device info."""
        try:
            # Extract info from bluesky-queueserver's device format
            classname = device_info.get('classname', 'Unknown')
            module = device_info.get('module', 'unknown')
            device_class = f"{module}.{classname}" if module != 'unknown' else classname
            
            # Determine device type from classname
            classname_lower = classname.lower()
            if 'motor' in classname_lower:
                device_type = 'motor'
            elif 'detector' in classname_lower or 'camera' in classname_lower:
                device_type = 'detector'
            elif 'signal' in classname_lower:
                device_type = 'signal'
            elif 'flyer' in classname_lower:
                device_type = 'flyer'
            else:
                device_type = 'device'
            
            # Get capabilities from existing format
            capabilities = {
                'readable': device_info.get('is_readable', False),
                'movable': device_info.get('is_movable', False),
                'flyable': device_info.get('is_flyable', False)
            }
            
            return DeviceDefinition(
                name=name,
                device_class=device_class,
                device_type=device_type,
                module=module,
                capabilities=capabilities
            )
            
        except Exception as e:
            logger.warning(f"Failed to create definition for {name}: {e}")
            return None
    
    def export_device_config(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Export device definitions in JSON format for external integrations.
        This is the unique functionality not available in base bluesky-queueserver.
        """
        config = {
            'devices': {},
            'device_registry': {
                'version': '1.0',
                'source': 'bluesky_queueserver'
            }
        }
        
        for name, device_def in self._device_definitions.items():
            config['devices'][name] = device_def.to_dict()
        
        if output_path:
            with open(output_path, 'w') as f:
                json.dump(config, f, indent=2)
                
        return config
    
    def get_device_names(self) -> List[str]:
        """Get list of all device names."""
        return list(self._device_definitions.keys())
    
    def get_device(self, name: str) -> Optional[DeviceDefinition]:
        """Get specific device definition."""
        return self._device_definitions.get(name)
    
    def get_devices_by_type(self, device_type: str) -> Dict[str, DeviceDefinition]:
        """Get devices of specific type."""
        return {
            name: device for name, device in self._device_definitions.items()
            if device.device_type == device_type
        }


def create_device_configs(profile_collection_path: str, 
                         output_dir: str,
                         beamline_name: str = None,
                         export_oaas: bool = True) -> Dict[str, str]:
    """
    Create device configuration files using bluesky-queueserver's existing functionality.
    
    Parameters
    ----------
    profile_collection_path : str
        Path to the profile collection
    output_dir : str  
        Directory to save configuration files
    beamline_name : str, optional
        Beamline identifier for file naming
    export_oaas : bool, optional
        Whether to export OaaS format (default: True)
        
    Returns
    -------
    dict
        Paths to generated configuration files
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Use bluesky-queueserver's existing functionality for the main export
    startup_path = Path(profile_collection_path) / "startup" if (Path(profile_collection_path) / "startup").exists() else Path(profile_collection_path)
    namespace = load_profile_collection(startup_path, patch_profiles=True, keep_re=False)
    existing_plans, existing_devices, plans_in_nspace, devices_in_nspace = existing_plans_and_devices_from_nspace(
        nspace=namespace, max_depth=0
    )
    
    # Generate file names
    prefix = f"{beamline_name}_" if beamline_name else ""
    queueserver_config = output_path / f"{prefix}devices_queueserver.yaml"
    
    # Use bluesky-queueserver's existing export
    save_existing_plans_and_devices(
        existing_plans=existing_plans,
        existing_devices=existing_devices,
        file_dir=str(output_path),
        file_name=queueserver_config.name,
        overwrite=True
    )
    
    result = {
        'queueserver_config': str(queueserver_config),
        'device_count': len(existing_devices)
    }
    
    # Export device config format if requested (unique functionality)
    if export_oaas:
        utils = DeviceExportUtilities()
        utils.load_devices_from_profile(profile_collection_path)
        oaas_config = output_path / f"{prefix}devices_oaas.json"
        utils.export_device_config(str(oaas_config))
        result['oaas_config'] = str(oaas_config)
    
    return result
