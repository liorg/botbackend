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
   where id = p_id
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

-- ── service_role only ────────────────────────────────────────────────────
revoke all on function public.rpc_otp_request(uuid, text, text, int, int, int, int) from public, anon, authenticated;
revoke all on function public.rpc_otp_claim(int)                                   from public, anon, authenticated;
revoke all on function public.rpc_otp_mark_sent(uuid, text, text)                  from public, anon, authenticated;
revoke all on function public.rpc_otp_verify(uuid, text)                           from public, anon, authenticated;
grant execute on function public.rpc_otp_request(uuid, text, text, int, int, int, int) to service_role;
grant execute on function public.rpc_otp_claim(int)                                   to service_role;
grant execute on function public.rpc_otp_mark_sent(uuid, text, text)                  to service_role;
grant execute on function public.rpc_otp_verify(uuid, text)                           to service_role;
