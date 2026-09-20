// Si el servidor responde "sesión requerida" (venció o nunca hubo), vuelve al login.
// Sin esto la pantalla quedaría vacía o con errores sueltos al vencer la sesión.
(function () {
  const fetchOriginal = window.fetch;
  const loginPorPagina = { '/director': '/director-login', '/turismo': '/turismo-login' };
  window.fetch = async function (...args) {
    const res = await fetchOriginal.apply(this, args);
    if (res.status === 401 && res.headers.get('X-Auth-Required') === '1') {
      try { localStorage.removeItem('vendeSeguro_session'); } catch (_) {}
      const destino = loginPorPagina[location.pathname] || '/login';
      console.log('[auth] sesión vencida, redirigiendo a', destino);
      if (location.pathname !== destino) location.replace(destino);
    }
    return res;
  };
})();
