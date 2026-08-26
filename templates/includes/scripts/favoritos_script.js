document.addEventListener("DOMContentLoaded", function () {
    // Lógica de Favoritar Global
    document.querySelectorAll('.btn-favorite').forEach(btn => {
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();

            const eventoId = this.getAttribute('data-evento-id');

            fetch('/favoritar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ evento_id: eventoId })
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    this.classList.toggle('active', data.favoritado);
                    
                    // Atualiza contador do header dinamicamente
                    const badge = document.querySelector('.btn-icon-header .badge');
                    if (badge && data.total_favoritos !== undefined) {
                        badge.textContent = data.total_favoritos;
                    }
                } else if (data.redirect) {
                    window.location.href = data.redirect;
                }
            })
            .catch(err => console.error("Erro ao favoritar:", err));
        });
    });
});