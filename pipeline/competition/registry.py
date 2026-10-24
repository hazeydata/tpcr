"""Challenger Registry.

Loads and validates challenger definitions from the challengers/ directory.
Each challenger is a Python module that declares what differs from baseline.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class ChallengerDefinition:
    """A loaded challenger definition with validation."""
    
    def __init__(self, name: str, module: Any):
        self.name = name
        self.module = module
        
        # Required attributes
        self.description = getattr(module, 'DESCRIPTION', '')
        self.date_registered = getattr(module, 'DATE_REGISTERED', '')
        self.hyperparams = getattr(module, 'HYPERPARAMS', {})
        self.weighting = getattr(module, 'WEIGHTING', {})
        
        # Optional attributes
        self.features = getattr(module, 'FEATURES', None)  # None = use baseline
        self.geo_decay_halflife = getattr(module, 'GEO_DECAY_HALFLIFE', 730)
        self.status = getattr(module, 'STATUS', 'active')  # active | retired | flagged
        
        # Validate
        self._validate()
    
    def _validate(self):
        """Validate that required fields are present."""
        if not self.name:
            raise ValueError(f"Challenger missing NAME attribute")
        if not self.description:
            raise ValueError(f"Challenger {self.name} missing DESCRIPTION")
        if not isinstance(self.hyperparams, dict):
            raise ValueError(f"Challenger {self.name} HYPERPARAMS must be a dict")
    
    def get_hyperparam(self, param_name: str, default: Any = None) -> Any:
        """Get a hyperparameter value, with fallback to default."""
        return self.hyperparams.get(param_name, default)
    
    def has_weighting_changes(self) -> bool:
        """Check if this challenger changes weighting vs baseline."""
        return bool(self.weighting)
        
    def get_weighting_method(self) -> str:
        """Get weighting method (e.g., 'inverse_freq')."""
        return self.weighting.get('method', 'baseline')
    
    def __repr__(self) -> str:
        return f"ChallengerDefinition(name='{self.name}', hyperparams={len(self.hyperparams)} changes)"


class ChallengerRegistry:
    """Registry for loading and managing challenger definitions."""
    
    def __init__(self):
        self.challengers: Dict[str, ChallengerDefinition] = {}
        self._challengers_dir: Optional[Path] = None
    
    def discover_challengers(self, challengers_dir: Path) -> None:
        """Discover and load all challenger modules from directory."""
        self._challengers_dir = challengers_dir
        
        if not challengers_dir.exists():
            print(f"Warning: Challengers directory not found: {challengers_dir}")
            return
            
        # Add challengers directory to Python path for imports
        challengers_parent = str(challengers_dir.parent)
        if challengers_parent not in sys.path:
            sys.path.insert(0, challengers_parent)
        
        # Find all .py files (excluding __init__.py)
        for py_file in challengers_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
                
            challenger_name = py_file.stem
            try:
                self._load_challenger(challenger_name)
            except Exception as e:
                print(f"Warning: Failed to load challenger {challenger_name}: {e}")
    
    def _load_challenger(self, challenger_name: str) -> None:
        """Load a specific challenger module."""
        module_name = f"pipeline.competition.challengers.{challenger_name}"
        
        try:
            # Import the module
            if module_name in sys.modules:
                # Reload if already imported
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)
            
            # Create challenger definition
            challenger = ChallengerDefinition(challenger_name, module)
            self.challengers[challenger_name] = challenger
            
            print(f"Loaded challenger: {challenger_name}")
            
        except Exception as e:
            raise RuntimeError(f"Failed to load challenger {challenger_name}: {e}")
    
    def get_challenger(self, name: str) -> ChallengerDefinition:
        """Get a loaded challenger by name."""
        if name not in self.challengers:
            raise ValueError(f"Challenger '{name}' not found. Available: {list(self.challengers.keys())}")
        return self.challengers[name]
    
    def list_challengers(self) -> List[str]:
        """List names of all loaded challengers."""
        return list(self.challengers.keys())
    
    def active_challengers(self) -> List[str]:
        """Names of loaded challengers that are not retired."""
        return [n for n, c in self.challengers.items() if c.status != 'retired']

    def validate_challenger(self, name: str) -> bool:
        """Validate that a challenger is properly defined."""
        try:
            challenger = self.get_challenger(name)
            # Basic validation already done in ChallengerDefinition.__init__
            return True
        except Exception:
            return False


def load_registry(challengers_dir: Path) -> ChallengerRegistry:
    """Create and populate a challenger registry."""
    registry = ChallengerRegistry()
    registry.discover_challengers(challengers_dir)
    return registry