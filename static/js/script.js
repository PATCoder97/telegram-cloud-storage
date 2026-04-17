// Auto-dismiss alerts
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(function() {
        let alerts = document.querySelectorAll('.alert');
        alerts.forEach(function(alert) {
            alert.classList.add('alert-fading-out');
            setTimeout(() => {
                if (typeof bootstrap !== 'undefined') {
                    new bootstrap.Alert(alert).close();
                }
            }, 500);
        });
    }, 3000);
});
