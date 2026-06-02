// YT Cut — Frontend JavaScript
(function() {
    'use strict';

    // ─── Utility ─────────────────────────────────────
    window.logout = function() {
        fetch('/api/auth/logout', { method: 'POST' })
            .then(() => { window.location.href = '/'; });
    };

    // ─── Modal ───────────────────────────────────────
    window.closeModal = function() {
        const m = document.getElementById('cut-modal');
        if (m) m.style.display = 'none';
    };

    // Close modal on background click
    document.addEventListener('click', function(e) {
        const modal = document.getElementById('cut-modal');
        if (modal && e.target === modal) {
            modal.style.display = 'none';
        }
    });

    // ─── Enter key in URL inputs ─────────────────────
    document.querySelectorAll('input[type="url"]').forEach(function(input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                const btn = this.closest('.download-box')?.querySelector('button');
                if (btn && typeof btn.onclick === 'function') btn.click();
            }
        });
    });
})();
