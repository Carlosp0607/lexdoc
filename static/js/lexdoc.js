// Ruta: static/js/lexdoc.js
// Comportamiento de la interfaz de LexDoc.
// Todo el JavaScript vive aqui, sin codigo en linea en el HTML, para que la
// politica CSP pueda bloquear cualquier script inyectado (script-src 'self').

document.addEventListener('click', function (e) {
    // Mostrar u ocultar contraseña: <button data-toggle-pass="id_del_input">
    var toggle = e.target.closest('[data-toggle-pass]');
    if (toggle) {
        var input = document.getElementById(toggle.dataset.togglePass);
        var oculta = input.type === 'password';
        input.type = oculta ? 'text' : 'password';
        toggle.textContent = oculta ? 'Ocultar' : 'Mostrar';
        return;
    }

    // Opcion seleccionable dentro de un grupo: <label data-grupo="nombre">
    var opcion = e.target.closest('[data-grupo]');
    if (opcion) {
        document.querySelectorAll('[data-grupo="' + opcion.dataset.grupo + '"]')
            .forEach(function (o) { o.classList.remove('selected'); });
        opcion.classList.add('selected');
        return;
    }

    // Area que abre el selector de archivo: <div data-abrir-archivo="id_input">
    var area = e.target.closest('[data-abrir-archivo]');
    if (area && e.target.type !== 'file') {
        document.getElementById(area.dataset.abrirArchivo).click();
    }
});

// Mostrar el nombre del archivo elegido: <input data-mostrar-archivo="id_destino">
document.addEventListener('change', function (e) {
    if (e.target.matches('[data-mostrar-archivo]')) {
        var nombre = e.target.files[0] ? e.target.files[0].name : '';
        document.getElementById(e.target.dataset.mostrarArchivo).textContent =
            nombre ? 'Archivo seleccionado: ' + nombre : '';
    }
});

// Confirmacion antes de enviar: <form data-confirmar="mensaje">
document.addEventListener('submit', function (e) {
    var mensaje = e.target.dataset.confirmar;
    if (mensaje && !window.confirm(mensaje)) {
        e.preventDefault();
    }
});
