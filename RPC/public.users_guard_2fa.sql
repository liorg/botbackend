
-- ── Guard: the 2FA flag is managed by the server only ──────────────────────
-- 1) A mobile number change resets verification — on every path (API, supabase-js, SQL).
-- 2) Only service_role may set verified=true. Without this, a user could
--    update their own row from supabase-js and mark themselves as verified.
create or replace function public.users_guard_2fa()
returns trigger
language plpgsql
as $$
begin
  if new.mobile is distinct from old.mobile then
    new.two_factor_verified    := false;
    new.two_factor_verified_at := null;
    return new;
  end if;

  if coalesce(auth.role(), '') <> 'service_role' then
    new.two_factor_verified    := old.two_factor_verified;
    new.two_factor_verified_at := old.two_factor_verified_at;
  end if;

  return new;
end;
$$;
