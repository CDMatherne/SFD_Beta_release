#!/usr/bin/env python3
"""
ML Course Prediction Integration Module for SFD Project

This module provides integration between the ML Course Prediction system
and the Advanced Analysis GUI.
"""

import os
import sys
import logging
import traceback
from pathlib import Path
from typing import Dict, Optional, Tuple, Any
import pandas as pd
import numpy as np

# Configure logger
logger = logging.getLogger(__name__)

# Try to import ML dependencies
ML_PREDICTION_AVAILABLE = False
try:
    import torch
    from pathlib import Path
    
    # Add ml_course_prediction to path
    ml_course_prediction_path = Path(__file__).parent / "ml_course_prediction"
    if str(ml_course_prediction_path.parent) not in sys.path:
        sys.path.insert(0, str(ml_course_prediction_path.parent))
    
    from ml_course_prediction.models.model_factory import load_model
    from ml_course_prediction.utils.feature_engineering import FeatureEngineer
    from ml_course_prediction.utils.trajectory_utils import TrajectoryProcessor
    
    ML_PREDICTION_AVAILABLE = True
except ImportError as e:
    logger.warning(f"ML Course Prediction module not available: {e}")
    ML_PREDICTION_AVAILABLE = False


class MLPredictionError(Exception):
    """Custom exception for ML prediction errors"""
    pass


class MLPredictionIntegrator:
    """
    Integrates ML Course Prediction with the Advanced Analysis system.
    """
    
    def __init__(self, model_path: Optional[str] = None, device: Optional[str] = None):
        """
        Initialize the ML Prediction Integrator.
        
        Args:
            model_path: Path to trained model. If None, will search for best_model.pt
            device: Device to use ('cpu' or 'cuda'). If None, auto-detects.
        """
        if not ML_PREDICTION_AVAILABLE:
            raise MLPredictionError("ML Course Prediction module is not available. "
                                  "Please install PyTorch and ensure ml_course_prediction module is available.")
        
        self.model = None
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model_path = model_path
        self.feature_engineer = FeatureEngineer()
        self.trajectory_processor = TrajectoryProcessor(
            max_gap_hours=6.0,
            min_trajectory_points=2,
            min_trajectory_hours=1.0
        )
        
    def load_model(self, model_path: Optional[str] = None) -> None:
        """
        Load the trained ML model.
        
        Args:
            model_path: Path to model file. If None, searches for best_model.pt
        """
        if model_path:
            self.model_path = model_path
        else:
            # Search for model
            model_dir = Path(__file__).parent / "ml_course_prediction" / "models" / "trained"
            best_model = model_dir / "best_model.pt"
            
            if best_model.exists():
                self.model_path = str(best_model)
            else:
                # Try checkpoint files
                checkpoint_files = list(model_dir.glob("checkpoint_*.pt"))
                if checkpoint_files:
                    # Use the latest checkpoint
                    self.model_path = str(sorted(checkpoint_files)[-1])
                else:
                    raise MLPredictionError(
                        f"Trained model not found in {model_dir}. "
                        "Please train a model first or specify a model path."
                    )
        
        if not os.path.exists(self.model_path):
            raise MLPredictionError(f"Model file not found: {self.model_path}")
        
        try:
            logger.info(f"Loading model from {self.model_path} on device {self.device}")
            self.model = load_model(self.model_path, device=self.device)
            self.model.eval()
            logger.info("Model loaded successfully")
        except Exception as e:
            raise MLPredictionError(f"Failed to load model: {str(e)}")
    
    def prepare_vessel_data(self, df: pd.DataFrame, mmsi: int, 
                          hours_back: int = 24) -> pd.DataFrame:
        """
        Prepare vessel data for prediction.
        
        Args:
            df: DataFrame with AIS data
            mmsi: Vessel MMSI number
            hours_back: Number of hours of historical data to use
            
        Returns:
            DataFrame with prepared vessel trajectory
            
        Raises:
            MLPredictionError: If data preparation fails
        """
        if df.empty:
            raise MLPredictionError("Input DataFrame is empty")
        
        # Filter for the specific vessel
        if 'MMSI' in df.columns:
            # Ensure MMSI types match
            sample_mmsi = df['MMSI'].iloc[0] if len(df) > 0 else None
            if sample_mmsi is not None:
                if isinstance(sample_mmsi, (int, np.integer)):
                    mmsi = int(mmsi)
                elif isinstance(sample_mmsi, (str, np.str_)):
                    mmsi = str(mmsi)
            df['MMSI'] = df['MMSI'].astype(type(mmsi))
        
        vessel_data = df[df['MMSI'] == mmsi].copy()
        
        if vessel_data.empty:
            raise MLPredictionError(f"No data found for vessel {mmsi}")
        
        # Sort by time and get last N hours of data
        if 'BaseDateTime' in vessel_data.columns:
            if not pd.api.types.is_datetime64_any_dtype(vessel_data['BaseDateTime']):
                vessel_data['BaseDateTime'] = pd.to_datetime(
                    vessel_data['BaseDateTime'], errors='coerce'
                )
            vessel_data = vessel_data.sort_values('BaseDateTime')
            
            # Get last N hours
            last_time = vessel_data['BaseDateTime'].max()
            time_window_start = last_time - pd.Timedelta(hours=hours_back)
            recent_data = vessel_data[vessel_data['BaseDateTime'] >= time_window_start].copy()
        else:
            # If no BaseDateTime, use all available data (take last 8 points max)
            recent_data = vessel_data.tail(8).copy()
        
        if len(recent_data) < 2:
            raise MLPredictionError(
                f"Insufficient data for prediction. Need at least 2 data points, "
                f"found {len(recent_data)}"
            )
        
        return recent_data
    
    def process_trajectory(self, vessel_data: pd.DataFrame) -> pd.DataFrame:
        """
        Process vessel trajectory data.
        
        Args:
            vessel_data: DataFrame with vessel AIS data
            
        Returns:
            Processed trajectory DataFrame
            
        Raises:
            MLPredictionError: If trajectory processing fails
        """
        try:
            # Segment trajectories
            trajectories = self.trajectory_processor.segment_trajectories(
                vessel_data,
                mmsi_col='MMSI',
                time_col='BaseDateTime'
            )
            
            if not trajectories:
                raise MLPredictionError("Failed to process vessel trajectory")
            
            # Use the most recent trajectory
            trajectory = trajectories[-1]
            
            if len(trajectory) < 2:
                raise MLPredictionError(
                    f"Insufficient trajectory points. Need at least 2, found {len(trajectory)}"
                )
            
            return trajectory
            
        except Exception as e:
            raise MLPredictionError(f"Trajectory processing failed: {str(e)}")
    
    def extract_features(self, trajectory: pd.DataFrame) -> torch.Tensor:
        """
        Extract features from trajectory for model input.
        
        Args:
            trajectory: Processed trajectory DataFrame
            
        Returns:
            Input tensor for the model
            
        Raises:
            MLPredictionError: If feature extraction fails
        """
        try:
            sequence_data = self.feature_engineer.create_sequence_features(
                trajectory,
                sequence_length=24,
                prediction_horizon=48
            )
            
            if not sequence_data or 'input_sequence' not in sequence_data:
                raise MLPredictionError("Failed to extract features from trajectory")
            
            input_sequence = sequence_data['input_sequence']
            
            # Convert to tensor
            input_tensor = torch.FloatTensor(input_sequence).unsqueeze(0)  # Add batch dimension
            
            return input_tensor
            
        except Exception as e:
            raise MLPredictionError(f"Feature extraction failed: {str(e)}")
    
    def predict(self, input_tensor: torch.Tensor) -> Dict[str, np.ndarray]:
        """
        Generate predictions using the loaded model.
        
        Args:
            input_tensor: Input tensor for the model
            
        Returns:
            Dictionary with prediction results:
            - position_mean: Predicted positions (time_steps, 2) [lat, lon]
            - position_std: Position uncertainties (time_steps, 2)
            - position_lower: Lower 68% confidence bounds (time_steps, 2)
            - position_upper: Upper 68% confidence bounds (time_steps, 2)
            - speed: Predicted speeds (time_steps,) [optional]
            - course: Predicted courses (time_steps,) [optional]
            
        Raises:
            MLPredictionError: If prediction fails
        """
        if self.model is None:
            self.load_model()
        
        try:
            with torch.no_grad():
                predictions = self.model.predict(input_tensor, return_uncertainty=True)
            
            # Extract predictions and convert to numpy
            result = {
                'position_mean': predictions['position']['mean'].cpu().numpy()[0],
                'position_std': predictions['position']['std'].cpu().numpy()[0],
                'position_lower': predictions['position']['lower_68'].cpu().numpy()[0],
                'position_upper': predictions['position']['upper_68'].cpu().numpy()[0],
            }
            
            # Add optional predictions
            if 'speed' in predictions:
                result['speed'] = predictions['speed'].cpu().numpy()[0]
            if 'course' in predictions:
                result['course'] = predictions['course'].cpu().numpy()[0]
            
            return result
            
        except Exception as e:
            raise MLPredictionError(f"Prediction failed: {str(e)}")
    
    def predict_vessel_course(self, df: pd.DataFrame, mmsi: int, 
                             hours_back: int = 24) -> Dict[str, Any]:
        """
        Complete prediction pipeline for a vessel.
        
        Args:
            df: DataFrame with AIS data
            mmsi: Vessel MMSI number
            hours_back: Number of hours of historical data to use
            
        Returns:
            Dictionary with:
            - predictions: Prediction results (see predict() method)
            - trajectory: Processed trajectory DataFrame
            - last_position: Tuple of (lat, lon) for last known position
            - last_time: Last known timestamp
            
        Raises:
            MLPredictionError: If any step fails
        """
        # Load model if not already loaded
        if self.model is None:
            self.load_model()
        
        # Prepare vessel data
        vessel_data = self.prepare_vessel_data(df, mmsi, hours_back)
        
        # Process trajectory
        trajectory = self.process_trajectory(vessel_data)
        
        # Extract features
        input_tensor = self.extract_features(trajectory)
        
        # Generate predictions
        predictions = self.predict(input_tensor)
        
        # Get last known position
        last_lat = float(trajectory['LAT'].iloc[-1])
        last_lon = float(trajectory['LON'].iloc[-1])
        last_time = None
        if 'BaseDateTime' in trajectory.columns:
            last_time = trajectory['BaseDateTime'].iloc[-1]
        
        return {
            'predictions': predictions,
            'trajectory': trajectory,
            'last_position': (last_lat, last_lon),
            'last_time': last_time,
            'mmsi': mmsi
        }


def is_available() -> bool:
    """Check if ML prediction is available."""
    return ML_PREDICTION_AVAILABLE


def create_integrator(model_path: Optional[str] = None, 
                     device: Optional[str] = None) -> MLPredictionIntegrator:
    """
    Factory function to create an MLPredictionIntegrator instance.
    
    Args:
        model_path: Path to trained model
        device: Device to use ('cpu' or 'cuda')
        
    Returns:
        MLPredictionIntegrator instance
        
    Raises:
        MLPredictionError: If module is not available
    """
    if not ML_PREDICTION_AVAILABLE:
        raise MLPredictionError("ML Course Prediction module is not available")
    
    return MLPredictionIntegrator(model_path=model_path, device=device)