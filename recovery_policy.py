def recovery_error(kind):
    value = ''.join(c for c in str(kind or '').lower() if c.isalnum())
    return value in ('quota','login','usagelimitexceeded','ratelimitexceeded','unauthorized','authenticationerror')

def proxy_session(session):
    provider = session.get('model_provider')
    return bool((provider and provider not in ('openai','codex')) or str(session.get('model','')).startswith('google-antigravity/'))


def recoverable_goal(session, usage_limited=True):
    goal=session.get('goal_status')
    return goal in ((None,'active','usage_limited') if usage_limited else (None,'active')) or bool(
        session.get('_continue_on_stop') and goal in ('blocked','complete','completed'))
