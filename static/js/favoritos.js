document.addEventListener("click", function (e) {
    const btn = e.target.closest('.btn-favorite');
    if (!btn) return;

    e.preventDefault();
    e.stopPropagation();

    const eventoId = btn.getAttribute('data-evento-id');

    fetch("/favoritar", {
        method: 'POST',
        headers: { 
            'Content-Type': 'application/json' 
        },
        body: JSON.stringify({ evento_id: eventoId })
    })
    .then(res => {
        if (res.status === 401 || res.redirected) {
            window.location.href = "/login";
            return;
        }
        return res.json();
    })
    .then(data => {
        if (data && data.status === 'success') {
            btn.classList.toggle('active', data.favoritado);
            
            // Seleciona especificamente o badge de favoritos
            const badge = document.querySelector('.badge-favoritos');
            if (badge && data.total_favoritos !== undefined) {
                badge.textContent = data.total_favoritos;
            }

            // Tratamento dinâmico para remoção do card na tela de favoritos
            if (!data.favoritado && window.location.pathname.includes('/meus-favoritos')) {
                const card = btn.closest('.ticket-card');
                if (card) card.remove();
                
                if (document.querySelectorAll('.ticket-card').length === 0) {
                    window.location.reload();
                }
            }
        }
    })
    .catch(err => console.error("Erro ao favoritar evento:", err));
});
