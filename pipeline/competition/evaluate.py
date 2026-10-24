"""Competition Evaluation.

Compares challenger forecasts vs baseline forecasts vs actuals for head-to-head analysis.
Produces daily comparison reports showing which model performs better.

Usage:
    python -m pipeline.competition.evaluate --challenger hypertuned_v1 --output-base ~/hazeydata/pipeline
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.competition.config import load_competition_config
from pipeline.competition.registry import load_registry


def load_baseline_forecasts(baseline_cfg: PipelineConfig) -> pd.DataFrame | None:
    """Load baseline forecast data."""
    baseline_forecast_path = baseline_cfg.forecast_dir / "all_forecasts.parquet"
    if not baseline_forecast_path.exists():
        # Try legacy filename
        baseline_forecast_path = baseline_cfg.forecast_dir / "all_forecasts_v3.parquet"
    
    if baseline_forecast_path.exists():
        return pd.read_parquet(baseline_forecast_path)
    return None


def load_challenger_forecasts(competition_cfg, challenger_name: str) -> pd.DataFrame | None:
    """Load challenger forecast data."""
    challenger_forecasts_dir = competition_cfg.get_challenger_forecasts_dir(challenger_name)
    challenger_forecast_path = challenger_forecasts_dir / f"all_forecasts_{challenger_name}.parquet"
    
    if challenger_forecast_path.exists():
        return pd.read_parquet(challenger_forecast_path)
    return None


def load_actuals_data(baseline_cfg: PipelineConfig, days_back: int = 7) -> pd.DataFrame | None:
    """Load actual wait times for comparison."""
    # Look for actuals in the standard locations
    actuals_paths = [
        baseline_cfg.output_base / "fact_tables" / "parquet" / "fact_actuals.parquet",
        baseline_cfg.output_base / "fact_tables" / "fact_actuals.parquet",
        baseline_cfg.output_base / "actuals.parquet"
    ]
    
    for actuals_path in actuals_paths:
        if actuals_path.exists():
            df = pd.read_parquet(actuals_path)
            # Filter to recent dates for comparison
            cutoff_date = date.today() - timedelta(days=days_back)
            df['park_date'] = pd.to_datetime(df['park_date']).dt.date
            df = df[df['park_date'] >= cutoff_date]
            return df
    
    return None


def calculate_entity_metrics(entity_df: pd.DataFrame) -> dict:
    """Calculate MAE, bias, and other metrics for a single entity."""
    if len(entity_df) == 0:
        return {}
    
    baseline_mae = np.mean(np.abs(entity_df['actual'] - entity_df['baseline_pred']))
    challenger_mae = np.mean(np.abs(entity_df['actual'] - entity_df['challenger_pred']))
    
    baseline_bias = np.mean(entity_df['baseline_pred'] - entity_df['actual'])
    challenger_bias = np.mean(entity_df['challenger_pred'] - entity_df['actual'])
    
    return {
        'n_samples': len(entity_df),
        'baseline_mae': baseline_mae,
        'challenger_mae': challenger_mae,
        'baseline_bias': baseline_bias,
        'challenger_bias': challenger_bias,
        'mae_delta': challenger_mae - baseline_mae,
        'mae_pct_change': (challenger_mae - baseline_mae) / baseline_mae * 100 if baseline_mae > 0 else 0,
        'challenger_wins': challenger_mae < baseline_mae
    }


def compare_forecasts(baseline_df: pd.DataFrame, challenger_df: pd.DataFrame, 
                     actuals_df: pd.DataFrame, challenger_name: str) -> dict:
    """Compare baseline vs challenger forecasts against actuals."""
    
    # Standardize column names for joining
    baseline_df = baseline_df.copy()
    challenger_df = challenger_df.copy()
    actuals_df = actuals_df.copy()
    
    # Create common join keys
    for df in [baseline_df, challenger_df, actuals_df]:
        df['park_date'] = pd.to_datetime(df['park_date']).dt.date
        df['join_key'] = df['entity_code'] + '|' + df['park_date'].astype(str) + '|' + df['time_slot'].astype(str)
    
    # Join all datasets
    comparison = baseline_df[['join_key', 'entity_code', 'park_date', 'time_slot', 'predicted_actual']].rename(
        columns={'predicted_actual': 'baseline_pred'}
    )
    
    challenger_data = challenger_df[['join_key', 'predicted_actual']].rename(
        columns={'predicted_actual': 'challenger_pred'}
    )
    
    actuals_data = actuals_df[['join_key', 'actual_wait']].rename(
        columns={'actual_wait': 'actual'}
    )
    
    # Perform joins
    comparison = comparison.merge(challenger_data, on='join_key', how='inner')
    comparison = comparison.merge(actuals_data, on='join_key', how='inner')
    
    if len(comparison) == 0:
        return {
            'error': 'No matching data found between baseline, challenger, and actuals',
            'baseline_rows': len(baseline_df),
            'challenger_rows': len(challenger_df), 
            'actuals_rows': len(actuals_df)
        }
    
    # Calculate overall metrics
    baseline_mae = np.mean(np.abs(comparison['actual'] - comparison['baseline_pred']))
    challenger_mae = np.mean(np.abs(comparison['actual'] - comparison['challenger_pred']))
    
    baseline_bias = np.mean(comparison['baseline_pred'] - comparison['actual'])
    challenger_bias = np.mean(comparison['challenger_pred'] - comparison['actual'])
    
    # Calculate per-entity metrics
    entity_metrics = {}
    entities_challenger_wins = 0
    entities_baseline_wins = 0
    
    for entity_code in comparison['entity_code'].unique():
        entity_df = comparison[comparison['entity_code'] == entity_code]
        metrics = calculate_entity_metrics(entity_df)
        if metrics:
            entity_metrics[entity_code] = metrics
            if metrics['challenger_wins']:
                entities_challenger_wins += 1
            else:
                entities_baseline_wins += 1
    
    # Per-park rollup
    comparison['park_code'] = comparison['entity_code'].str[:2]
    park_metrics = {}
    
    for park_code in comparison['park_code'].unique():
        park_df = comparison[comparison['park_code'] == park_code]
        park_baseline_mae = np.mean(np.abs(park_df['actual'] - park_df['baseline_pred']))
        park_challenger_mae = np.mean(np.abs(park_df['actual'] - park_df['challenger_pred']))
        
        park_metrics[park_code] = {
            'baseline_mae': park_baseline_mae,
            'challenger_mae': park_challenger_mae,
            'mae_delta': park_challenger_mae - park_baseline_mae,
            'challenger_wins': park_challenger_mae < park_baseline_mae,
            'n_samples': len(park_df)
        }
    
    # Find worst entities for challenger
    worst_entities = sorted(
        [(k, v) for k, v in entity_metrics.items() if not v['challenger_wins']], 
        key=lambda x: x[1]['mae_delta'], 
        reverse=True
    )[:10]
    
    # Find best entities for challenger  
    best_entities = sorted(
        [(k, v) for k, v in entity_metrics.items() if v['challenger_wins']],
        key=lambda x: x[1]['mae_delta']
    )[:10]
    
    return {
        'comparison_date': date.today().isoformat(),
        'challenger_name': challenger_name,
        'data_summary': {
            'total_comparisons': len(comparison),
            'entities_compared': len(entity_metrics),
            'parks_compared': len(park_metrics),
            'date_range': {
                'start': comparison['park_date'].min().isoformat(),
                'end': comparison['park_date'].max().isoformat()
            }
        },
        'overall_metrics': {
            'baseline_mae': round(baseline_mae, 1),
            'challenger_mae': round(challenger_mae, 1),
            'mae_delta': round(challenger_mae - baseline_mae, 1),
            'mae_pct_change': round((challenger_mae - baseline_mae) / baseline_mae * 100, 1) if baseline_mae > 0 else 0,
            'baseline_bias': round(baseline_bias, 1),
            'challenger_bias': round(challenger_bias, 1),
            'challenger_wins_overall': challenger_mae < baseline_mae
        },
        'entity_summary': {
            'entities_challenger_wins': entities_challenger_wins,
            'entities_baseline_wins': entities_baseline_wins,
            'challenger_win_rate': round(entities_challenger_wins / len(entity_metrics) * 100, 1) if entity_metrics else 0
        },
        'park_metrics': park_metrics,
        'worst_entities_for_challenger': [
            {
                'entity_code': entity_code,
                'baseline_mae': round(metrics['baseline_mae'], 1),
                'challenger_mae': round(metrics['challenger_mae'], 1),
                'mae_delta': round(metrics['mae_delta'], 1),
                'mae_pct_change': round(metrics['mae_pct_change'], 1)
            }
            for entity_code, metrics in worst_entities
        ],
        'best_entities_for_challenger': [
            {
                'entity_code': entity_code,
                'baseline_mae': round(metrics['baseline_mae'], 1),
                'challenger_mae': round(metrics['challenger_mae'], 1),
                'mae_delta': round(metrics['mae_delta'], 1),
                'mae_pct_change': round(metrics['mae_pct_change'], 1)
            }
            for entity_code, metrics in best_entities
        ]
    }


def run_evaluation(challenger_name: str, output_base: Path, days_back: int = 7) -> dict:
    """Run head-to-head evaluation between challenger and baseline."""
    
    # Load configurations
    baseline_cfg = PipelineConfig(output_base=output_base)
    competition_cfg = load_competition_config(output_base)
    
    # Set up logging
    log = PipelineLogger(f'evaluate_{challenger_name}', competition_cfg.logs_dir)
    
    log.info("=" * 60)
    log.info(f"COMPETITION EVALUATION: {challenger_name} vs baseline")
    log.info("=" * 60)
    
    # Load data
    with log.timed("load baseline forecasts"):
        baseline_df = load_baseline_forecasts(baseline_cfg)
        if baseline_df is None:
            raise ValueError("No baseline forecasts found")
        log.info(f"Loaded {len(baseline_df):,} baseline predictions")
    
    with log.timed("load challenger forecasts"):
        challenger_df = load_challenger_forecasts(competition_cfg, challenger_name)
        if challenger_df is None:
            raise ValueError(f"No challenger forecasts found for {challenger_name}")
        log.info(f"Loaded {len(challenger_df):,} challenger predictions")
    
    with log.timed("load actuals"):
        actuals_df = load_actuals_data(baseline_cfg, days_back)
        if actuals_df is None:
            raise ValueError("No actuals data found for comparison")
        log.info(f"Loaded {len(actuals_df):,} actual observations (last {days_back} days)")
    
    # Run comparison
    with log.timed("compare forecasts"):
        comparison_data = compare_forecasts(baseline_df, challenger_df, actuals_df, challenger_name)
    
    if 'error' in comparison_data:
        log.error(f"Comparison failed: {comparison_data['error']}")
        return comparison_data
    
    # Save results
    report_date = date.today().isoformat()
    output_file = competition_cfg.reports_dir / f"comparison_{report_date}.json"
    
    with open(output_file, 'w') as f:
        json.dump(comparison_data, f, indent=2, default=str)
    
    log.info(f"Comparison report saved to {output_file}")
    
    # Log key results
    overall = comparison_data['overall_metrics']
    entity_summary = comparison_data['entity_summary']
    
    log.info("=" * 60)
    log.info("EVALUATION RESULTS:")
    log.info(f"  Baseline MAE: {overall['baseline_mae']} min")
    log.info(f"  Challenger MAE: {overall['challenger_mae']} min")
    log.info(f"  Delta: {overall['mae_delta']:+.1f} min ({overall['mae_pct_change']:+.1f}%)")
    log.info(f"  Challenger wins: {entity_summary['entities_challenger_wins']}/{comparison_data['data_summary']['entities_compared']} entities ({entity_summary['challenger_win_rate']:.1f}%)")
    log.info("=" * 60)
    
    return comparison_data


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Evaluate challenger vs baseline")
    parser.add_argument("--challenger", required=True, help="Challenger name (e.g., hypertuned_v1)")
    parser.add_argument("--output-base", type=Path, required=True, help="Pipeline output base directory")
    parser.add_argument("--days-back", type=int, default=7, help="Days of actuals to compare against")
    
    args = parser.parse_args()
    
    try:
        result = run_evaluation(args.challenger, args.output_base, args.days_back)
        
        print(f"\n✅ Evaluation completed: challenger {'WINS' if result.get('overall_metrics', {}).get('challenger_wins_overall') else 'loses'}")
        return 0
    except Exception as e:
        print(f"❌ Evaluation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # Add repo root to Python path for imports
    repo_root = Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    
    sys.exit(main())