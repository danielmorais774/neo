//! As marcações e os recortes do print do navegador local.
//!
//! Mora numa crate SEPARADA por desempenho, não por organização: o `tauri dev`
//! compila a crate do desktop sem otimização, e as funções de imagem são
//! genéricas — compiladas onde são usadas. Dentro do `browser_local.rs`, um
//! print de tela Retina levava ~5 s só para ser reduzido e marcado (medido em
//! 17/11/2026, `neo-core/docs/browser-visao-confirmacao-plan.md` §7.2); aqui,
//! com `[profile.dev.package.neo-imagem] opt-level = 3`, leva ~0,14 s.
//!
//! A interface não tem genérico nenhum (bytes entram, bytes saem), para que
//! nada daqui volte a ser compilado do lado de lá.

/// Um elemento numerado do snapshot: o `ref` e a caixa em px de CSS.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Marca {
    pub numero: i64,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// Dígitos 3x5. Um "font" de 10 linhas evita puxar renderização de texto de
/// verdade só para escrever números de dois dígitos numa etiqueta.
const GLYPHS: [[u8; 5]; 10] = [
    [0b111, 0b101, 0b101, 0b101, 0b111], // 0
    [0b010, 0b110, 0b010, 0b010, 0b111], // 1
    [0b111, 0b001, 0b111, 0b100, 0b111], // 2
    [0b111, 0b001, 0b111, 0b001, 0b111], // 3
    [0b101, 0b101, 0b111, 0b001, 0b001], // 4
    [0b111, 0b100, 0b111, 0b001, 0b111], // 5
    [0b111, 0b100, 0b111, 0b101, 0b111], // 6
    [0b111, 0b001, 0b001, 0b001, 0b001], // 7
    [0b111, 0b101, 0b111, 0b101, 0b111], // 8
    [0b111, 0b101, 0b111, 0b001, 0b111], // 9
];

const MARK: [u8; 3] = [255, 45, 149];
const INK: [u8; 3] = [255, 255, 255];

fn put(img: &mut image::RgbImage, x: i64, y: i64, color: [u8; 3]) {
    if x < 0 || y < 0 || x >= img.width() as i64 || y >= img.height() as i64 {
        return;
    }
    img.put_pixel(x as u32, y as u32, image::Rgb(color));
}

fn fill(img: &mut image::RgbImage, x: i64, y: i64, w: i64, h: i64, color: [u8; 3]) {
    for dy in 0..h {
        for dx in 0..w {
            put(img, x + dx, y + dy, color);
        }
    }
}

/// Contorno de 2px — não preenche, senão esconde o que o modelo precisa ler.
fn outline(img: &mut image::RgbImage, x: i64, y: i64, w: i64, h: i64, color: [u8; 3]) {
    for t in 0..2 {
        fill(img, x + t, y + t, w - 2 * t, 1, color);
        fill(img, x + t, y + h - 1 - t, w - 2 * t, 1, color);
        fill(img, x + t, y + t, 1, h - 2 * t, color);
        fill(img, x + w - 1 - t, y + t, 1, h - 2 * t, color);
    }
}

fn digits(img: &mut image::RgbImage, x: i64, y: i64, scale: i64, value: i64) {
    let text = value.to_string();
    let mut cursor = x;
    for ch in text.chars() {
        let glyph = GLYPHS[ch.to_digit(10).unwrap_or(0) as usize];
        for (row, bits) in glyph.iter().enumerate() {
            for col in 0..3 {
                if bits & (1 << (2 - col)) != 0 {
                    fill(
                        img,
                        cursor + col * scale,
                        y + row as i64 * scale,
                        scale,
                        scale,
                        INK,
                    );
                }
            }
        }
        cursor += 4 * scale;
    }
}

/// Escreve os números dos `ref` em cima do print. Devolve `None` em qualquer
/// tropeço (jpeg estranho, viewport zerada): print sem marcação ainda é útil,
/// print nenhum não é.
pub fn draw_marks(jpeg: &[u8], rects: &[Marca], css_width: f64, scale: f64) -> Option<Vec<u8>> {
    if (rects.is_empty() || css_width <= 0.0) && scale >= 0.999 {
        return None;
    }
    let decoded = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg).ok()?;
    let mut img = decoded.to_rgb8();

    // Reduzir AQUI e não no `clip` do CDP: pedir clip faz o Chrome relayoutar a
    // página para a captura, e o usuário vê a tela piscar e os elementos
    // mudarem de tamanho a cada passo (25 vezes por tarefa). Redimensionar o
    // JPEG depois não toca no navegador dele.
    if scale < 0.999 {
        let (w, h) = (
            ((img.width() as f64 * scale).round() as u32).max(1),
            ((img.height() as f64 * scale).round() as u32).max(1),
        );
        img = image::imageops::resize(&img, w, h, image::imageops::FilterType::Triangle);
    }
    if rects.is_empty() || css_width <= 0.0 {
        let mut out = Vec::with_capacity(jpeg.len());
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 70)
            .encode(img.as_raw(), img.width(), img.height(), image::ExtendedColorType::Rgb8)
            .ok()?;
        return Some(out);
    }
    // A imagem pode vir em DPR 2 (retina) ou reduzida pelo `clip.scale` — o
    // fator sai da largura real contra a largura em px de CSS.
    let ratio = img.width() as f64 / css_width;
    let scale = ((ratio * 2.0).round() as i64).clamp(2, 6);
    let glyph_h = 5 * scale;

    for rect in rects {
        let number = rect.numero;
        if number < 0 {
            continue;
        }
        let at = |v: f64| (v * ratio).round() as i64;
        let (x, y, w, h) = (at(rect.x), at(rect.y), at(rect.w), at(rect.h));
        outline(&mut img, x, y, w, h, MARK);

        // Etiqueta acima do elemento; se não couber, por dentro dele.
        let label_w = (number.to_string().len() as i64) * 4 * scale + scale;
        let label_h = glyph_h + 2 * scale;
        let label_y = if y - label_h >= 0 { y - label_h } else { y };
        fill(&mut img, x, label_y, label_w, label_h, MARK);
        digits(&mut img, x + scale, label_y + scale, scale, number);
    }

    let mut out = Vec::with_capacity(jpeg.len());
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 70)
        .encode(img.as_raw(), img.width(), img.height(), image::ExtendedColorType::Rgb8)
        .ok()?;
    Some(out)
}

/// Recorta o print num retângulo em px de CSS. Devolve `None` em qualquer
/// tropeço — print inteiro ainda serve, print nenhum não.
///
/// Existe para a TRIAGEM DE PAINEL: quando um modal aparece, a pergunta é "o que
/// está escrito aqui dentro?", e a tela inteira é o pior insumo possível para
/// isso. O print normal sai a `scale: 0.75, quality: 60` de uma tela de 2954px —
/// um modal de 600px vira texto ilegível. Aqui não há redução nem marcação: o
/// recorte sai na resolução nativa e com qualidade alta, porque o trabalho dele
/// é ser LIDO.
///
/// O recorte é feito no JPEG já capturado, nunca pelo `clip` do CDP — pedir clip
/// ao Chrome faz a página relayoutar e o usuário vê a tela piscar (é o mesmo
/// motivo documentado em `draw_marks`).
pub fn crop_jpeg(jpeg: &[u8], css_width: f64, rect: &[f64; 4]) -> Option<Vec<u8>> {
    if css_width <= 0.0 {
        return None;
    }
    let decoded = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg).ok()?;
    let img = decoded.to_rgb8();
    let ratio = img.width() as f64 / css_width;
    // Uma folga em volta: sem ela o X de fechar, que costuma morar em cima da
    // borda, sai cortado pela metade — e é justamente ele que a triagem precisa
    // descrever quando o veredito é "intruso".
    let pad = 12.0 * ratio;
    let x = ((rect[0] * ratio) - pad).max(0.0).round() as u32;
    let y = ((rect[1] * ratio) - pad).max(0.0).round() as u32;
    let w = ((rect[2] * ratio) + 2.0 * pad).round() as u32;
    let h = ((rect[3] * ratio) + 2.0 * pad).round() as u32;
    let w = w.min(img.width().saturating_sub(x));
    let h = h.min(img.height().saturating_sub(y));
    if w < 16 || h < 16 {
        return None;
    }
    let cortado = image::imageops::crop_imm(&img, x, y, w, h).to_image();
    let mut out = Vec::with_capacity(jpeg.len());
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 82)
        .encode(
            cortado.as_raw(),
            cortado.width(),
            cortado.height(),
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
    Some(out)
}

/// Reduz o print até `largura_max` px de largura (mantendo a proporção) e
/// recodifica. `None` quando já cabe ou em qualquer tropeço — aí vai a captura
/// crua, que ainda serve.
///
/// É o print da CONFIRMAÇÃO visual: medido em 17/11 com o `glm-5.3-flash`, a
/// tela a 1276 px leu o formulário inteiro igual à de 2553 px, com 1/4 dos
/// tokens, ~0,7 s mais rápida e 1/3 dos bytes na rede.
pub fn reduzir(jpeg: &[u8], largura_max: u32, qualidade: u8) -> Option<Vec<u8>> {
    let decoded = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg).ok()?;
    if largura_max == 0 || decoded.width() <= largura_max {
        return None;
    }
    let img = decoded.to_rgb8();
    let altura = ((img.height() as u64 * largura_max as u64) / img.width() as u64).max(1) as u32;
    let menor = image::imageops::resize(&img, largura_max, altura, image::imageops::FilterType::Triangle);
    let mut out = Vec::with_capacity(jpeg.len() / 2);
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), qualidade.clamp(20, 95))
        .encode(menor.as_raw(), menor.width(), menor.height(), image::ExtendedColorType::Rgb8)
        .ok()?;
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn jpeg(w: u32, h: u32) -> Vec<u8> {
        let img = image::RgbImage::from_pixel(w, h, image::Rgb([200, 210, 220]));
        let mut out = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 80)
            .encode(img.as_raw(), w, h, image::ExtendedColorType::Rgb8)
            .unwrap();
        out
    }

    #[test]
    fn reduz_pela_largura_e_mantem_a_proporcao() {
        let menor = reduzir(&jpeg(3000, 1500), 1280, 70).unwrap();
        let img = image::load_from_memory_with_format(&menor, image::ImageFormat::Jpeg).unwrap();
        assert_eq!((img.width(), img.height()), (1280, 640));
        assert!(reduzir(&jpeg(1000, 500), 1280, 70).is_none(), "já cabe: vai cru");
        assert!(reduzir(b"nao e jpeg", 1280, 70).is_none());
    }
}
