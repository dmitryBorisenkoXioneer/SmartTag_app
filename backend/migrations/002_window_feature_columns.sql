-- Add compact window feature columns for multi-feature IF training/inference.

ALTER TABLE feature_windows
ADD COLUMN IF NOT EXISTS std_mag DOUBLE PRECISION,
ADD COLUMN IF NOT EXISTS peak_to_peak_mag DOUBLE PRECISION,
ADD COLUMN IF NOT EXISTS crest_factor DOUBLE PRECISION;
