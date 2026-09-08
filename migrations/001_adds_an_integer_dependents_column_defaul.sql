-- drafted by agent: Adds an integer dependents column (default 0) to public.employees with a CHECK constraint limiting values to 0–10, using idempotent guards.
ALTER TABLE public.employees ADD COLUMN IF NOT EXISTS dependents integer NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'employees_dependents_range' AND conrelid = 'public.employees'::regclass
  ) THEN
    ALTER TABLE public.employees ADD CONSTRAINT employees_dependents_range CHECK (dependents >= 0 AND dependents <= 10);
  END IF;
END $$;
