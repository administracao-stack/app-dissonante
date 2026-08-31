document.addEventListener("click", function (e) {
    // Procura pelo botão .btn-favorite mais próximo do elemento clicado
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
            
            // Atualiza o contador de favoritos no header
            const badge = document.querySelector('.btn-icon-header .badge');
            if (badge && data.total_favoritos !== undefined) {
                badge.textContent = data.total_favoritos;
            }

            // Se estiver na página de meus favoritos e o evento for desfavoritado, remove o card
            if (!data.favoritado && window.location.pathname.includes('/meus-favoritos')) {
                const card = btn.closest('.ticket-card');
                if (card) card.remove();
                
                // Se não houver mais cards visíveis, recarrega a página para exibir o Empty State do Jinja
                if (document.querySelectorAll('.ticket-card').length === 0) {
                    window.location.reload();
                }
            }
        }
    })
    .catch(err => console.error("Erro ao favoritar evento:", err));
});
