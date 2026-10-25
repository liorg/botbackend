-- trusted_domains — דומיינים שמותר ל-fetch בתוך סקריפט Deno
-- נכתב רק אחרי אימות: https, DNS ציבורי, תעודה תקפה (services/trusted_domains.py)
-- נקרא ע"י ה-provisioner → env TRUSTED_DOMAINS פר worker

create table if not exists public.trusted_domains (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.users(id) on delete cascade,
    domain          text not null,
    source_url      text,
    status          text not null default 'verified',
    cert_issuer     text,
    cert_expires_at timestamptz,
    resolved_ips    text[] not null default '{}',
    last_error      text,
    verified_at     timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint trusted_domains_status_chk check (status in ('verified', 'revoked', 'failed')),
    -- אותו regex כמו ב-Python / Go / C#. מגן גם על ה-env וה-label של docker
    constraint trusted_domains_domain_chk check (
        length(domain) <= 253
        and domain ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
    ),
    constraint trusted_domains_user_domain_uq unique (user_id, domain)
);

create index if not exists trusted_domains_verified_idx
    on public.trusted_domains (user_id) where status = 'verified';

alter table public.trusted_domains enable row level security;
revoke all on table public.trusted_domains from anon, authenticated;


-- ── upsert אחרי אימות מוצלח ────────────────────────────────────────────
create or replace function public.rpc_trusted_domain_upsert(
    p_user_id         uuid,
    p_domain          text,
    p_source_url      text,
    p_cert_issuer     text,
    p_cert_expires_at timestamptz,
    p_resolved_ips    text[]
) returns public.trusted_domains
language plpgsql security definer set search_path = public as $$
#variable_conflict use_column
declare
    r public.trusted_domains;
begin
    insert into trusted_domains (
        user_id, domain, source_url, status, cert_issuer, cert_expires_at,
        resolved_ips, last_error, verified_at, created_at, updated_at
    ) values (
        p_user_id, lower(p_domain), p_source_url, 'verified', p_cert_issuer, p_cert_expires_at,
        coalesce(p_resolved_ips, '{}'), null, now(), now(), now()
    )
    on conflict (user_id, domain) do update set
        source_url      = excluded.source_url,
        status          = 'verified',
        cert_issuer     = excluded.cert_issuer,
        cert_expires_at = excluded.cert_expires_at,
        resolved_ips    = excluded.resolved_ips,
        last_error      = null,
        verified_at     = now(),
        updated_at      = now()
    returning * into r;
    return r;
end $$;


-- ── update_settings שולח רשימה מלאה: מה שלא בה → revoked ───────────────
create or replace function public.rpc_trusted_domain_revoke_missing(
    p_user_id uuid,
    p_keep    text[]
) returns integer
language plpgsql security definer set search_path = public as $$
declare
    n integer;
begin
    update trusted_domains
       set status = 'revoked', updated_at = now()
     where user_id = p_user_id
       and status <> 'revoked'
       and not (domain = any (coalesce(p_keep, '{}')));
    get diagnostics n = row_count;
    return n;
end $$;


create or replace function public.rpc_trusted_domain_revoke(
    p_user_id uuid,
    p_domain  text
) returns boolean
language plpgsql security definer set search_path = public as $$
begin
    update trusted_domains
       set status = 'revoked', updated_at = now()
     where user_id = p_user_id
       and domain = lower(p_domain)
       and status <> 'revoked';
    return found;
end $$;


create or replace function public.rpc_trusted_domain_list(
    p_user_id uuid
) returns setof public.trusted_domains
language plpgsql stable security definer set search_path = public as $$
begin
    return query
        select * from trusted_domains
         where user_id = p_user_id and status <> 'revoked'
         order by domain;
end $$;


revoke all on function public.rpc_trusted_domain_upsert(uuid, text, text, text, timestamptz, text[]) from public, anon, authenticated;
revoke all on function public.rpc_trusted_domain_revoke_missing(uuid, text[])                       from public, anon, authenticated;
revoke all on function public.rpc_trusted_domain_revoke(uuid, text)                                 from public, anon, authenticated;
revoke all on function public.rpc_trusted_domain_list(uuid)                                         from public, anon, authenticated;

grant execute on function public.rpc_trusted_domain_upsert(uuid, text, text, text, timestamptz, text[]) to service_role;
grant execute on function public.rpc_trusted_domain_revoke_missing(uuid, text[])                       to service_role;
grant execute on function public.rpc_trusted_domain_revoke(uuid, text)                                 to service_role;
grant execute on function public.rpc_trusted_domain_list(uuid)                                         to service_role;
