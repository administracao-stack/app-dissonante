<script>
document.addEventListener("DOMContentLoaded", function () {
    const favoriteButtons = document.querySelectorAll('.btn-favorite');

    favoriteButtons.forEach(btn => {
        btn.addEventListener('click', function (e) {
            // Trava imediatamente a navegação da tag <a> ancestral e a propagação no DOM
            e.preventDefault();
            e.stopPropagation();

            const eventoId = this.getAttribute('data-evento-id');

            fetch("{{ url_for('favoritar') }}", {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json' 
                },
                body: JSON.stringify({ evento_id: eventoId })
            })
            .then(res => {
                if (res.status === 401) {
                    window.location.href = "{{ url_for('login') }}";
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    // Alterna o estado de ativo no botão
                    this.classList.toggle('active', data.favoritado);
                    
                    // Atualiza o contador no header (ícone de coração) se existir
                    const badge = document.querySelector('.btn-icon-header .badge');
                    if (badge && data.total_favoritos !== undefined) {
                        badge.textContent = data.total_favoritos;
                    }
                }
            })
            .catch(err => console.error("Erro ao favoritar evento:", err));
        });
    });
});
</script>
