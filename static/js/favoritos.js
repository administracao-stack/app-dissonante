document.addEventListener("DOMContentLoaded", function () {
    const favoriteButtons = document.querySelectorAll('.btn-favorite');

    favoriteButtons.forEach(btn => {
        // O argumento `true` no final ativa a fase de captura do evento, 
        // interceptando o clique antes do elemento pai <a> navegar.
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();

            const eventoId = this.getAttribute('data-evento-id');

            fetch("/favoritar", {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json' 
                },
                body: JSON.stringify({ evento_id: eventoId })
            })
            .then(res => {
                if (res.status === 401) {
                    // Redireciona para o login caso não esteja autenticado
                    window.location.href = "/login";
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    // Alterna o estado visual do botão
                    this.classList.toggle('active', data.favoritado);
                    
                    // Atualiza o contador de favoritos no header (se existir no DOM)
                    const badge = document.querySelector('.btn-icon-header .badge');
                    if (badge && data.total_favoritos !== undefined) {
                        badge.textContent = data.total_favoritos;
                    }
                }
            })
            .catch(err => console.error("Erro ao favoritar evento:", err));
        }, true);
    });
});