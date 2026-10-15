<?php
// usp_respaldo.php — Respaldo por correo de los archivos que llegan al WhatsApp de USP (HUNO®)
//
// Subir a huno.cl/api/usp_respaldo.php (cPanel). Lo llama el agente de WhatsApp (Railway)
// una vez al día con los archivos que cumplen 170 días; al día 180 el agente los borra.
// Railway no permite enviar correo por SMTP en el plan Hobby: por eso el envío pasa por aquí.
//
// Seguridad: solo envía a DESTINO (no se puede usar para mandar correos a otras personas)
// y exige el encabezado X-USP-Token con el mismo valor que RESPALDO_TOKEN en Railway.
//
// Si los correos grandes fallan, sube post_max_size a 32M en cPanel → MultiPHP INI Editor.

declare(strict_types=1);

const TOKEN = 'PEGAR_AQUI_RESPALDO_TOKEN';   // mínimo 32 caracteres; el mismo de Railway
const DESTINO = 'alertas@huno.cl';
const REMITENTE = 'alertas@huno.cl';
const NOMBRE_REMITENTE = 'Agente WhatsApp USP';
const MAX_ARCHIVOS = 80;
const MAX_BYTES = 20 * 1024 * 1024;

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

function salir(int $codigo, array $datos): void
{
    http_response_code($codigo);
    echo json_encode($datos, JSON_UNESCAPED_UNICODE);
    exit;
}

function cabecera_utf8(string $texto): string
{
    return '=?UTF-8?B?' . base64_encode($texto) . '?=';
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    salir(405, ['ok' => false, 'error' => 'Solo POST']);
}
$recibido = (string)($_SERVER['HTTP_X_USP_TOKEN'] ?? '');
if (strlen(TOKEN) < 32 || strpos(TOKEN, 'PEGAR_AQUI') === 0 || !hash_equals(TOKEN, $recibido)) {
    salir(401, ['ok' => false, 'error' => 'Token incorrecto o sin configurar']);
}

$crudo = file_get_contents('php://input');
$datos = json_decode($crudo === false ? '' : $crudo, true);
if (!is_array($datos)) {
    salir(400, ['ok' => false, 'error' => 'JSON inválido o vacío (¿post_max_size muy bajo?)']);
}

$asunto = trim(preg_replace('/[\r\n]+/', ' ', (string)($datos['asunto'] ?? 'Respaldo WhatsApp USP')));
$asunto = substr($asunto !== '' ? $asunto : 'Respaldo WhatsApp USP', 0, 240);
$html = (string)($datos['html'] ?? '');
$archivos = $datos['archivos'] ?? [];
if (!is_array($archivos) || count($archivos) > MAX_ARCHIVOS) {
    salir(400, ['ok' => false, 'error' => 'Lista de archivos inválida']);
}

$limite = 'usp_' . bin2hex(random_bytes(12));
$cuerpo = "This is a multi-part message in MIME format.\r\n\r\n"
    . "--$limite\r\nContent-Type: text/html; charset=UTF-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    . chunk_split(base64_encode($html));
$total = 0;
foreach ($archivos as $a) {
    if (!is_array($a)) {
        salir(400, ['ok' => false, 'error' => 'Archivo inválido']);
    }
    $nombre = substr(preg_replace('/[^A-Za-z0-9._-]/', '_', (string)($a['nombre'] ?? 'archivo')), 0, 120);
    $nombre = $nombre !== '' ? $nombre : 'archivo';
    $mime = (string)($a['mime'] ?? '');
    if (!preg_match('#^[a-z0-9.+-]+/[a-z0-9.+-]+$#i', $mime)) {
        $mime = 'application/octet-stream';
    }
    $bin = base64_decode((string)($a['base64'] ?? ''), true);
    if ($bin === false) {
        salir(400, ['ok' => false, 'error' => "Archivo mal codificado: $nombre"]);
    }
    $total += strlen($bin);
    if ($total > MAX_BYTES) {
        salir(413, ['ok' => false, 'error' => 'El correo supera el tamaño máximo']);
    }
    $cuerpo .= "--$limite\r\nContent-Type: $mime; name=\"$nombre\"\r\nContent-Transfer-Encoding: base64\r\n"
        . "Content-Disposition: attachment; filename=\"$nombre\"\r\n\r\n" . chunk_split(base64_encode($bin));
}
$cuerpo .= "--$limite--\r\n";

$cabeceras = "MIME-Version: 1.0\r\n"
    . 'From: ' . cabecera_utf8(NOMBRE_REMITENTE) . ' <' . REMITENTE . ">\r\n"
    . "Content-Type: multipart/mixed; boundary=\"$limite\"";
$ok = mail(DESTINO, cabecera_utf8($asunto), $cuerpo, $cabeceras, '-f' . REMITENTE);

if (!$ok) {
    salir(500, ['ok' => false, 'error' => 'El servidor no pudo enviar el correo (mail)']);
}
salir(200, ['ok' => true, 'archivos' => count($archivos), 'bytes' => $total]);
