-- sql/otp_2fa.sql — OTP over WhatsApp for two_factor_verified
--
--   backend          → rpc_otp_request   row with status=pending (code + code_hash)
--   PhoneOrchestrator→ rpc_otp_claim     every 2s, takes pending → sending
--                      sends check_number through the prober
--                    → rpc_otp_mark_sent sent/failed, deletes code
--   backend          → rpc_otp_verify    against code_hash only

create or replace function public.norm_mobile(p text)
returns text language sql immutable as $$
  select case
           when d like '00%' then substr(d, 3)
           when d like '0%'  then '972' || substr(d, 2)
           else d
         end
  from (select regexp_replace(coalesce(p, ''), '\D', '', 'g') as d) s
$$;

drop table if exists public.otp_sender_status;
drop table if exists public.otp_requests cascade;

create table public.otp_requests (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,
  mobile        text not null,
  name          text,
  code          text,                          -- until sent only; deleted in mark_sent
  code_hash     text not null,                 -- HMAC(OTP_PEPPER, user_id:code) — for verify
  send_status   text not null default 'pending'
                check (send_status in ('pending','sending','sent','failed')),
  send_error    text,
  wa_message_id text,
  expires_at    timestamptz not null,
  attempts      int  not null default 0,
  max_attempts  int  not null default 5,
  consumed_at   timestamptz,
  created_at    timestamptz not null default now()
);

create index otp_requests_user_idx    on public.otp_requests (user_id, created_at desc);
create index otp_requests_pending_idx on public.otp_requests (created_at) where send_status = 'pending';

alter table public.otp_requests enable row level security;   -- no policies = service_role only

-- ── backend: new request ─────────────────────────────────────────────────
create or replace function public.rpc_otp_request(
  p_user_id uuid, p_code text, p_code_hash text,
  p_ttl_sec int, p_resend_sec int, p_max_per_hour int, p_max_attempts int
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  v_user   users%rowtype;
  v_mobile text;
  v_last   timestamptz;
  v_oldest timestamptz;
  v_cnt    int;
  v_id     uuid;
begin
  select * into v_user from users where id = p_user_id for update;
  if not found then return jsonb_build_object('ok', false, 'code', 'NO_USER'); end if;

  v_mobile := norm_mobile(v_user.mobile);
  if length(v_mobile) < 9 then return jsonb_build_object('ok', false, 'code', 'NO_MOBILE'); end if;
  if v_user.two_factor_verified then return jsonb_build_object('ok', false, 'code', 'ALREADY_VERIFIED'); end if;

  -- the 2FA mobile must not be one of this user's bot phones (rows from before the trigger)
  if exists (select 1 from phones p where p.user_id = p_user_id and norm_mobile(p.number) = v_mobile) then
    return jsonb_build_object('ok', false, 'code', 'MOBILE_IS_BOT_PHONE');
  end if;

  select max(created_at) into v_last from otp_requests
   where user_id = p_user_id and send_status <> 'failed';
  if v_last > now() - make_interval(secs => p_resend_sec) then
    return jsonb_build_object('ok', false, 'code', 'RESEND_TOO_SOON',
      'retry_in', ceil(extract(epoch from v_last + make_interval(secs => p_resend_sec) - now()))::int);
  end if;

  select count(*), min(created_at) into v_cnt, v_oldest from otp_requests
   where user_id = p_user_id and created_at > now() - interval '1 hour';
  if v_cnt >= p_max_per_hour then
    return jsonb_build_object('ok', false, 'code', 'RATE_LIMITED',
      'retry_in', ceil(extract(epoch from v_oldest + interval '1 hour' - now()))::int);
  end if;

  -- the previous code dies
  update otp_requests set expires_at = least(expires_at, now()), code = null
   where user_id = p_user_id and consumed_at is null and expires_at > now();

  insert into otp_requests (user_id, mobile, name, code, code_hash, expires_at, max_attempts)
  values (p_user_id, v_mobile, v_user.name, p_code, p_code_hash,
          now() + make_interval(secs => p_ttl_sec), p_max_attempts)
  returning id into v_id;

  return jsonb_build_object('ok', true, 'id', v_id, 'mobile', v_mobile);
end $$;

-- ── PhoneOrchestrator: take what's waiting ───────────────────────────────
create or replace function public.rpc_otp_claim(p_limit int default 10)
returns table (id uuid, mobile text, name text, code text)
language plpgsql security definer set search_path = public as $$
begin
  -- 'sending' longer than 2 minutes = the runner died between claim and mark_sent.
  -- Closed as failed: the user can request a new code right away.
  update otp_requests
     set send_status = 'failed',
         send_error  = coalesce(send_error, 'stuck in sending'),
         code        = null,
         expires_at  = least(expires_at, now())
   where send_status = 'sending' and created_at < now() - interval '2 minutes';

  return query
  update otp_requests o
     set send_status = 'sending'
   where o.id in (select x.id from otp_requests x
                   where x.send_status = 'pending' and x.expires_at > now() and x.code is not null
                   order by x.created_at limit p_limit
                   for update skip locked)
  returning o.id, o.mobile, o.name, o.code;
end $$;

-- ── PhoneOrchestrator: result ────────────────────────────────────────────
create or replace function public.rpc_otp_mark_sent(p_id uuid, p_wa_message_id text, p_error text)
returns void language sql security definer set search_path = public as $$
  update otp_requests
     set send_status   = case when p_error is null then 'sent' else 'failed' end,
         wa_message_id = p_wa_message_id,
         send_error    = p_error,
         code          = null,
         expires_at    = case when p_error is null then expires_at else now() end
   where id = p_id and send_status = 'sending'   -- a late write doesn't revive a row that was closed
$$;

-- ── backend: verify ──────────────────────────────────────────────────────
create or replace function public.rpc_otp_verify(p_user_id uuid, p_code_hash text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  r otp_requests%rowtype;
  v_at timestamptz := now();
begin
  select * into r from otp_requests
   where user_id = p_user_id and consumed_at is null and send_status = 'sent'
   order by created_at desc limit 1 for update;

  if not found                     then return jsonb_build_object('ok', false, 'code', 'NO_ACTIVE'); end if;
  if r.expires_at <= now()         then return jsonb_build_object('ok', false, 'code', 'EXPIRED');   end if;
  if r.attempts >= r.max_attempts  then return jsonb_build_object('ok', false, 'code', 'TOO_MANY');  end if;
  if (select norm_mobile(mobile) from users where id = p_user_id) is distinct from r.mobile then
    return jsonb_build_object('ok', false, 'code', 'MOBILE_CHANGED');
  end if;

  if r.code_hash <> p_code_hash then
    update otp_requests set attempts = attempts + 1 where id = r.id;
    return jsonb_build_object('ok', false, 'code', 'INVALID',
                              'remaining', greatest(r.max_attempts - r.attempts - 1, 0));
  end if;

  update otp_requests set consumed_at = v_at where id = r.id;
  update users set two_factor_verified = true, two_factor_verified_at = v_at where id = p_user_id;
  return jsonb_build_object('ok', true, 'verified_at', v_at);
end $$;

-- ── mobile changed → 2FA drops ───────────────────────────────────────────
create or replace function public.trg_users_mobile_reset_2fa()
returns trigger language plpgsql as $$
begin
  if norm_mobile(new.mobile) is distinct from norm_mobile(old.mobile) then
    -- the 2FA mobile is per user and must not be one of that user's bot phones.
    -- The other direction (provisioning that number as a bot phone) is blocked in routers/phones.py.
    if norm_mobile(new.mobile) <> '' and exists (
         select 1 from phones p
          where p.user_id = new.id and norm_mobile(p.number) = norm_mobile(new.mobile)) then
      raise exception 'MOBILE_IS_BOT_PHONE'
        using errcode = 'P0001',
              hint = 'This number is one of your bot phones and cannot be used for two-step verification';
    end if;

    new.two_factor_verified    := false;
    new.two_factor_verified_at := null;
    update otp_requests set expires_at = least(expires_at, now()), code = null
     where user_id = new.id and consumed_at is null;
  end if;
  return new;
end $$;

drop trigger if exists users_mobile_reset_2fa on public.users;
create trigger users_mobile_reset_2fa before update of mobile on public.users
  for each row execute function public.trg_users_mobile_reset_2fa();

-- ── provision: is this number the user's 2FA mobile? ─────────────────────
-- phones.py calls this before provisioning. Same norm_mobile as everywhere else,
-- so Python keeps no copy of the rule.
create or replace function public.rpc_is_2fa_mobile(p_user_id uuid, p_number text)
returns boolean language sql stable security definer set search_path = public as $$
  select coalesce((
    select norm_mobile(u.mobile) <> '' and norm_mobile(u.mobile) = norm_mobile(p_number)
      from users u
     where u.id = p_user_id), false)
$$;

-- ── service_role only ────────────────────────────────────────────────────
revoke all on function public.rpc_otp_request(uuid, text, text, int, int, int, int) from public, anon, authenticated;
revoke all on function public.rpc_otp_claim(int)                                   from public, anon, authenticated;
revoke all on function public.rpc_otp_mark_sent(uuid, text, text)                  from public, anon, authenticated;
revoke all on function public.rpc_otp_verify(uuid, text)                           from public, anon, authenticated;
grant execute on function public.rpc_otp_request(uuid, text, text, int, int, int, int) to service_role;
grant execute on function public.rpc_otp_claim(int)                                   to service_role;
grant execute on function public.rpc_otp_mark_sent(uuid, text, text)                  to service_role;
grant execute on function public.rpc_otp_verify(uuid, text)                           to service_role;
revoke all on function public.rpc_is_2fa_mobile(uuid, text) from public, anon, authenticated;
grant execute on function public.rpc_is_2fa_mobile(uuid, text) to service_role;
