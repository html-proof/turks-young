-- Account lifecycle authority.  Device splash state remains a client concern.
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS account_status TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_account_status_check;
ALTER TABLE users ADD CONSTRAINT users_account_status_check
  CHECK (account_status IN ('active', 'disabled', 'suspended', 'deleted'));

-- Preserve existing production onboarding state while moving authority to users.
UPDATE users u SET
  onboarding_completed = p.onboarding_completed,
  onboarding_completed_at = CASE WHEN p.onboarding_completed THEN COALESCE(u.updated_at, now()) END,
  updated_at = now()
FROM user_profiles p WHERE p.uid = u.uid;

CREATE INDEX IF NOT EXISTS users_account_status_idx ON users(account_status);
