"""Template context shared by every rendered page."""


def csp(request):
    """The per-request nonce the Content-Security-Policy allows inline scripts by.

    ``SecurityHeadersMiddleware`` sets it; pages put it on every inline
    ``<script>`` as ``nonce="{{ csp_nonce }}"``.
    """
    return {'csp_nonce': getattr(request, 'csp_nonce', '')}
