-- ══════════════════════════════════════════════════════════════════════════════
-- Vende Seguro — Migración v1: Schema multi-tenant + RLS
-- Ejecutar en: Supabase Dashboard → SQL Editor → New Query → Run
-- ══════════════════════════════════════════════════════════════════════════════

-- Extensión para UUIDs (ya está habilitada en Supabase por defecto, pero por si acaso)
create extension if not exists "uuid-ossp";


-- ──────────────────────────────────────────────────────────────────────────────
-- 1. TABLA: empresas
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists public.empresas (
  id                 uuid        primary key default uuid_generate_v4(),
  nombre_empresa     text        not null,
  cuit_empresa       text        not null unique,
  plan_suscripcion   text        not null default 'free'
                     check (plan_suscripcion in ('free', 'pro', 'enterprise')),
  activa             boolean     not null default true,
  created_at         timestamptz not null default now()
);

alter table public.empresas enable row level security;

-- Solo el service_role (backend) puede insertar/actualizar empresas
-- Los usuarios solo ven su propia empresa (a través del helper)
create policy "empresas_select_own" on public.empresas
  for select
  using (id = public.mi_empresa_id());


-- ──────────────────────────────────────────────────────────────────────────────
-- 2. FUNCIÓN HELPER: mi_empresa_id()
-- Debe crearse ANTES de las políticas que la usan
-- ──────────────────────────────────────────────────────────────────────────────
create or replace function public.mi_empresa_id()
returns uuid
language sql
stable
security definer
as $$
  select empresa_id
  from public.usuarios
  where id = auth.uid()
$$;


-- ──────────────────────────────────────────────────────────────────────────────
-- 3. TABLA: usuarios
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists public.usuarios (
  id          uuid    primary key references auth.users(id) on delete cascade,
  empresa_id  uuid    not null references public.empresas(id) on delete cascade,
  nombre      text    not null,
  rol         text    not null default 'vendedor'
              check (rol in ('admin', 'vendedor', 'supervisor')),
  email       text    not null unique,
  activo      boolean not null default true,
  created_at  timestamptz not null default now()
);

alter table public.usuarios enable row level security;

-- Cada usuario ve solo su propio perfil
create policy "usuarios_select_own" on public.usuarios
  for select
  using (id = auth.uid());

-- Admin puede ver todos los usuarios de su empresa
create policy "usuarios_select_empresa" on public.usuarios
  for select
  using (empresa_id = public.mi_empresa_id());

-- Solo service_role puede insertar usuarios (el registro lo hace el backend/admin)
-- Los usuarios pueden actualizar solo su propio nombre
create policy "usuarios_update_own" on public.usuarios
  for update
  using (id = auth.uid())
  with check (id = auth.uid());


-- ──────────────────────────────────────────────────────────────────────────────
-- 4. TABLA: clientes_cartera
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists public.clientes_cartera (
  id                    uuid        primary key default uuid_generate_v4(),
  empresa_id            uuid        not null references public.empresas(id) on delete cascade,
  vendedor_id           uuid        references public.usuarios(id) on delete set null,
  cuit                  text        not null,
  razon_social          text        not null,
  ciudad                text,
  limite_credito        numeric(14,2) not null default 0,
  saldo_deuda           numeric(14,2) not null default 0,
  score_calculado       smallint    check (score_calculado between 0 and 999),
  score_rango           text,
  peor_situacion_bcra   smallint    not null default 1
                        check (peor_situacion_bcra between 1 and 6),
  alerta                boolean     not null default false,
  oportunidad           boolean     not null default false,
  ultima_actualizacion  timestamptz default now(),
  created_at            timestamptz not null default now(),
  unique (empresa_id, cuit)
);

alter table public.clientes_cartera enable row level security;

create policy "cartera_select" on public.clientes_cartera
  for select
  using (empresa_id = public.mi_empresa_id());

create policy "cartera_insert" on public.clientes_cartera
  for insert
  with check (empresa_id = public.mi_empresa_id());

create policy "cartera_update" on public.clientes_cartera
  for update
  using (empresa_id = public.mi_empresa_id())
  with check (empresa_id = public.mi_empresa_id());

create policy "cartera_delete" on public.clientes_cartera
  for delete
  using (empresa_id = public.mi_empresa_id());

-- Índices de performance
create index if not exists idx_cartera_empresa on public.clientes_cartera(empresa_id);
create index if not exists idx_cartera_cuit     on public.clientes_cartera(cuit);
create index if not exists idx_cartera_vendedor on public.clientes_cartera(vendedor_id);


-- ──────────────────────────────────────────────────────────────────────────────
-- 5. TABLA: score_historial
-- ──────────────────────────────────────────────────────────────────────────────
create table if not exists public.score_historial (
  id            uuid        primary key default uuid_generate_v4(),
  empresa_id    uuid        not null references public.empresas(id) on delete cascade,
  cliente_id    uuid        not null references public.clientes_cartera(id) on delete cascade,
  score         smallint    not null,
  situacion     smallint    not null,
  calculado_at  timestamptz not null default now()
);

alter table public.score_historial enable row level security;

create policy "historial_select" on public.score_historial
  for select
  using (empresa_id = public.mi_empresa_id());

create policy "historial_insert" on public.score_historial
  for insert
  with check (empresa_id = public.mi_empresa_id());

-- Índices
create index if not exists idx_historial_empresa  on public.score_historial(empresa_id);
create index if not exists idx_historial_cliente  on public.score_historial(cliente_id);
create index if not exists idx_historial_fecha    on public.score_historial(calculado_at desc);


-- ──────────────────────────────────────────────────────────────────────────────
-- 6. FUNCIÓN TRIGGER: actualizar ultima_actualizacion en clientes_cartera
-- ──────────────────────────────────────────────────────────────────────────────
create or replace function public.set_ultima_actualizacion()
returns trigger
language plpgsql
as $$
begin
  new.ultima_actualizacion := now();
  return new;
end;
$$;

create trigger trg_cartera_updated
  before update on public.clientes_cartera
  for each row execute function public.set_ultima_actualizacion();


-- ──────────────────────────────────────────────────────────────────────────────
-- FIN — Verificación rápida (ejecutar por separado si querés confirmar)
-- ──────────────────────────────────────────────────────────────────────────────
-- select table_name from information_schema.tables
--   where table_schema = 'public'
--   order by table_name;
