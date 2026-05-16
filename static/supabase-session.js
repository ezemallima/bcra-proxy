// ── Supabase Auth Module — Vende Seguro ──────────────────────────────────────
// Requiere: @supabase/supabase-js v2 cargado antes de este script
// Configurar con las credenciales del proyecto Supabase

const SUPABASE_URL  = 'https://rurxkrhbmoiomdqbkavy.supabase.co';
const SUPABASE_ANON = 'sb_publishable_KE_-r2F760BVWRun_aqxhg_OloyoVW5';

let _supabase = null;
let _perfil   = null;  // { id, empresa_id, nombre, rol, email }

function getClient() {
  if (!_supabase) {
    _supabase = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
  }
  return _supabase;
}

// ── Login ────────────────────────────────────────────────────────────────────

async function loginConEmail(email, password) {
  const sb = getClient();
  const { data, error } = await sb.auth.signInWithPassword({ email, password });
  if (error) throw error;
  return data;
}

// ── Perfil del usuario autenticado ───────────────────────────────────────────

async function cargarPerfil() {
  if (_perfil) return _perfil;
  const sb = getClient();

  const { data: { user }, error: authErr } = await sb.auth.getUser();
  if (authErr || !user) throw new Error('Sin sesión activa');

  const { data, error } = await sb
    .from('usuarios')
    .select('id, empresa_id, nombre, rol, email, activo')
    .eq('id', user.id)
    .single();

  if (error || !data) throw new Error('Perfil de usuario no encontrado');
  if (!data.activo) throw new Error('Usuario desactivado');

  _perfil = data;
  return _perfil;
}

// ── Guard — bloquea rutas sin sesión ─────────────────────────────────────────

async function verificarSesion() {
  const sb = getClient();
  const { data: { session } } = await sb.auth.getSession();

  if (!session) {
    return null; // sin sesión Supabase — el guard decide si usar legacy o redirigir
  }

  try {
    return await cargarPerfil();
  } catch (e) {
    // No hacer signOut — el guard en comercial.html muestra el error inline
    // y le da al usuario la opción de cerrar sesión conscientemente
    throw e;
  }
}

// ── Logout ───────────────────────────────────────────────────────────────────

async function doLogout() {
  _perfil = null;
  await getClient().auth.signOut();
  window.location.replace('/login');
}

// ── Helpers para otros módulos ───────────────────────────────────────────────

function getPerfilCache() { return _perfil; }
function getEmpresaId()   { return _perfil?.empresa_id ?? null; }
