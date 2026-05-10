-- Price Memory table — stores learned market prices from every search
-- Run this in Supabase SQL Editor before first use

CREATE TABLE IF NOT EXISTS price_memory (
    id SERIAL PRIMARY KEY,
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    fuel TEXT,
    price_min INT,
    price_max INT,
    price_median INT,
    price_avg INT,
    sample_count INT DEFAULT 0,
    year_min INT,
    year_max INT,
    avg_mileage_km INT,
    last_updated TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'search',
    UNIQUE(make, model, fuel)
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_price_memory_make_model ON price_memory (lower(make), lower(model));

-- Enable RLS (Row Level Security) — allow service role full access
ALTER TABLE price_memory ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON price_memory FOR ALL USING (true) WITH CHECK (true);

-- Useful view: price guide formatted for AI prompt
CREATE OR REPLACE VIEW price_guide_view AS
SELECT
    make,
    model,
    fuel,
    price_min,
    price_max,
    price_median,
    sample_count,
    year_min,
    year_max,
    avg_mileage_km,
    last_updated
FROM price_memory
WHERE sample_count >= 2
ORDER BY sample_count DESC;
