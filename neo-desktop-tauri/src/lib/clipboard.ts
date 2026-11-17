// Copiar texto sem depender só do clipboard "moderno": no WKWebView do Tauri a
// origem é `tauri://localhost`, que nem sempre conta como secure context — e
// fora de secure context `navigator.clipboard` nem existe (em dev, no
// http://localhost:1420, existe). Por isso o fallback com <textarea> +
// execCommand, que ainda funciona em qualquer webview.

/** Copia `text` para a área de transferência. Retorna false se nenhuma via funcionou. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // permissão negada / documento sem foco → tenta a via antiga abaixo
  }
  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea');
  area.value = text;
  // fora da tela, mas ainda selecionável — `display: none` mataria a seleção
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  area.setAttribute('readonly', '');
  document.body.appendChild(area);
  try {
    area.select();
    area.setSelectionRange(0, area.value.length);
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}
