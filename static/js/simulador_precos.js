/* ==========================================================================
   Simulador de Preços Global
   ========================================================================== */
(function (global) {
    let prices = {};
    let stockLimits = {};
    let quantities = {};
    let fator1x = 1.0;
    let fator2x = 1.0;

    function formatBRL(value) {
        return value.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
    }

    function calculateTotal() {
        let totalPix = 0;
        for (const key in quantities) {
            totalPix += quantities[key] * (prices[key] || 0);
        }

        const btnAddCart = document.getElementById('btn-add-cart');

        if (totalPix > 0) {
            const totalCard1x = totalPix * fator1x;
            const totalCard2x = totalPix * fator2x;
            const parcela2x = totalCard2x / 2;

            const elPix = document.getElementById('display-total-pix');
            const elCard1x = document.getElementById('display-total-card1x');
            const elCard2x = document.getElementById('display-total-card2x');

            if (elPix) elPix.innerHTML = `${formatBRL(totalPix)} <span class="pix-badge-text">no Pix</span>`;
            if (elCard1x) elCard1x.innerText = `Cartão à vista: (${formatBRL(totalCard1x)})`;
            if (elCard2x) elCard2x.innerText = `Cartão 2x: (${formatBRL(totalCard2x)} ou 2x de ${formatBRL(parcela2x)})`;

            if (btnAddCart) btnAddCart.disabled = false;
        } else {
            const elPix = document.getElementById('display-total-pix');
            const elCard1x = document.getElementById('display-total-card1x');
            const elCard2x = document.getElementById('display-total-card2x');

            if (elPix) elPix.innerHTML = 'R$ 0,00 <span class="pix-badge-text">no Pix</span>';
            if (elCard1x) elCard1x.innerText = 'Cartão à vista: (R$ 0,00)';
            if (elCard2x) elCard2x.innerText = 'Cartão 2x: (R$ 0,00 ou 2x de R$ 0,00)';

            if (btnAddCart) btnAddCart.disabled = true;
        }
    }

    global.updateQty = function (type, change) {
        const currentQty = quantities[type] || 0;
        const maxAllowed = stockLimits[type] !== undefined ? stockLimits[type] : 99;
        const newQty = currentQty + change;

        if (newQty >= 0 && newQty <= maxAllowed) {
            quantities[type] = newQty;

            const qtyDisplay = document.getElementById(`qty-${type}`);
            const inputField = document.getElementById(`input-${type}`);

            if (qtyDisplay) qtyDisplay.innerText = quantities[type];
            if (inputField) inputField.value = quantities[type];

            calculateTotal();
        } else if (change > 0 && newQty > maxAllowed) {
            alert(maxAllowed === 0 ? 'Este lote está esgotado.' : `Resta(m) apenas ${maxAllowed} ingresso(s) disponível(is).`);
        }
    };

    global.toggleAccordion = function (id) {
        const content = document.getElementById(id);
        const arrow = document.getElementById(`arrow-${id}`);
        if (content) content.classList.toggle('open');
        if (arrow) arrow.classList.toggle('rotated');
    };

    global.initSimulador = function (config) {
        prices = config.prices || {};
        stockLimits = config.stockLimits || {};
        fator1x = config.fator1x || 1.0;
        fator2x = config.fator2x || 1.0;

        // Inicializa zerado para cada chave de lote
        quantities = {};
        Object.keys(prices).forEach(key => {
            quantities[key] = 0;
        });

        // Validação no submit do formulário
        const form = document.getElementById('tickets-form');
        if (form) {
            form.addEventListener('submit', function (e) {
                const totalIngressos = Object.values(quantities).reduce((a, b) => a + b, 0);
                if (totalIngressos === 0) {
                    e.preventDefault();
                    alert('Selecione pelo menos um ingresso para prosseguir.');
                }
            });
        }
    };
})(window);