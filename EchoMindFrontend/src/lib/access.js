// Navigation is a presentation guard. The Python API independently authorizes
// each request with the current database membership.
export function identityKey(session) {
  return session ? [session.user_id || session.user?.id || '', session.organization_id || '', session.role || ''].join(':') : ''
}

export function workspaceAccess(session) {
  const authenticated = Boolean((session?.user_id || session?.user?.id) && session?.organization_id)
  const admin = authenticated && session.role === 'admin'
  const knowledge = authenticated && ['admin', 'kb_admin'].includes(session.role)
  return { authenticated, admin, knowledge, views: [
    'chat', ...(authenticated ? ['profile', 'tickets', 'answers'] : []),
    ...(knowledge ? ['knowledge'] : []), ...(admin ? ['admin', 'evaluation', 'diagnostics'] : [])
  ] }
}
