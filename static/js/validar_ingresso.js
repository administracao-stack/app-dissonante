document.addEventListener("DOMContentLoaded", function () {
    const btnScanner = document.getElementById("btn-abrir-scanner");
    const readerWrapper = document.getElementById("qr-reader-wrapper");
    const inputCodigo = document.getElementById("codigo");
    const formValidacao = document.getElementById("form-validacao");
    let html5QrcodeScanner = null;

    if (!btnScanner) return;

    btnScanner.addEventListener("click", function () {
        if (readerWrapper.style.display === "none" || !readerWrapper.style.display) {
            readerWrapper.style.display = "block";
            btnScanner.textContent = "Fechar Câmera";

            html5QrcodeScanner = new Html5QrcodeScanner("qr-reader", {
                fps: 10,
                qrbox: { width: 250, height: 250 }
            });

            html5QrcodeScanner.render((decodedText) => {
                // Ao ler o código QR com sucesso
                inputCodigo.value = decodedText;
                html5QrcodeScanner.clear();
                readerWrapper.style.display = "none";
                btnScanner.textContent = "📷 Ler QR Code pela Câmera";

                // Submete o formulário automaticamente após a leitura
                formValidacao.submit();
            }, (errorMessage) => {
                // Erros contínuos de scan ignorados para manter a busca ativa
            });
        } else {
            if (html5QrcodeScanner) {
                html5QrcodeScanner.clear();
            }
            readerWrapper.style.display = "none";
            btnScanner.textContent = "📷 Ler QR Code pela Câmera";
        }
    });
});