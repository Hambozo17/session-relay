def recovery_error(kind):
    value = ''.join(c for c in str(kind or '').lower() if c.isalnum())
    return value in ('quota','login','usagelimitexceeded','ratelimitexceeded','unauthorized','authenticationerror')

def proxy_session(session):
    provider = session.get('model_provider')
    return bool((provider and provider not in ('openai','codex')) or str(session.get('model','')).startswith('google-antigravity/'))
