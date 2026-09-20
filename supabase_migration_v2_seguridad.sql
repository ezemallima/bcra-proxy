-- Vende Seguro — migración v2 (seguridad). Ejecutar en Supabase → SQL Editor.
--
-- Problema: la política "usuarios_update_own" (v1) permite que cada usuario autenticado
-- actualice CUALQUIER columna de su propia fila usando la clave pública del navegador:
--   * rol         → podía subirse a 'admin'
--   * empresa_id  → podía apuntar a otra empresa y leer su cartera (las demás políticas
--                   filtran por mi_empresa_id(), que sale de esta misma tabla)
--   * activo      → podía reactivarse tras una baja
--   * nombre      → el backend usa el nombre para limitar a cada vendedor a su cartera
-- La app nunca escribe en esta tabla desde el navegador (solo la lee), así que se
-- elimina la posibilidad de editarla. Los cambios de perfil los hace el administrador
-- desde el panel de Supabase o con la clave service_role.

drop policy if exists "usuarios_update_own" on public.usuarios;
revoke update, insert, delete on public.usuarios from anon, authenticated;

-- Verificación (no debe listar ninguna política de tipo UPDATE/INSERT/DELETE para usuarios):
--   select policyname, cmd from pg_policies where schemaname = 'public' and tablename = 'usuarios';
--
-- Revisar además, en Authentication → Providers → Email, que "Allow new users to sign up"
-- esté desactivado si las cuentas las da de alta solo el administrador.
