document.addEventListener("DOMContentLoaded", function () {
    
    // Auxiliar para aplicar formatação via evento de 'input'
    function aplicarMascara(idElemento, funcaoMascara) {
        const el = document.getElementById(idElemento);
        if (!el) return;

        el.addEventListener("input", function (e) {
            e.target.value = funcaoMascara(e.target.value);
        });
    }

    // 1. Máscara de CPF (000.000.000-00)
    function mascaraCPF(valor) {
        return valor
            .replace(/\D/g, "")
            .slice(0, 11)
            .replace(/(\d{3})(\d)/, "$1.$2")
            .replace(/(\d{3})(\d)/, "$1.$2")
            .replace(/(\d{3})(\d{1,2})$/, "$1-$2");
    }

    // 2. Máscara de Telefone / WhatsApp ((85) 90000-0000)
    function mascaraTelefone(valor) {
        let v = valor.replace(/\D/g, "").slice(0, 11);
        if (v.length > 10) {
            return v.replace(/^(\d{2})(\d{5})(\d{4})$/, "($1) $2-$3");
        } else if (v.length > 5) {
            return v.replace(/^(\d{2})(\d{4})(\d{0,4})$/, "($1) $2-$3");
        } else if (v.length > 2) {
            return v.replace(/^(\d{2})(\d{0,5})$/, "($1) $2");
        }
        return v;
    }

    // 3. Máscara de Cartão de Crédito (0000 0000 0000 0000)
    function mascaraCartao(valor) {
        return valor
            .replace(/\D/g, "")
            .slice(0, 16)
            .replace(/(\d{4})(\d)/, "$1 $2")
            .replace(/(\d{4})(\d)/, "$1 $2")
            .replace(/(\d{4})(\d)/, "$1 $2");
    }

    // 4. Máscara de Validade do Cartão (MM/AA)
    function mascaraValidade(valor) {
        return valor
            .replace(/\D/g, "")
            .slice(0, 4)
            .replace(/(\d{2})(\d)/, "$1/$2");
    }

    // 5. Máscara para CVV (apenas números, máx 4 dígitos)
    function mascaraCVV(valor) {
        return valor.replace(/\D/g, "").slice(0, 4);
    }

    // Inicialização das máscaras nos inputs correspondentes
    aplicarMascara("cpf", mascaraCPF);
    aplicarMascara("telefone", mascaraTelefone);
    aplicarMascara("cardNumber", mascaraCartao);
    aplicarMascara("cardExpiration", mascaraValidade);
    aplicarMascara("securityCode", mascaraCVV);
});