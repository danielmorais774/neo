// Virtualização da lista do chat: bolha longe da tela DESMONTA (sai o
// ReactMarkdown, o highlight, as imagens em data URL) e fica só um slot vazio
// com a altura que ela tinha. Voltando a rolar pra perto, remonta.
//
// Por que slot com altura (e não uma janela de índices com espaçadores): as
// bolhas têm altura muito variável e mudam depois de montar (streaming, imagem
// que carrega, plano que atualiza). Guardando a altura REAL medida de cada uma,
// trocar bolha↔slot vazio não mexe em nada do layout — o scroll fica parado.
//
// Três observadores, todos com a própria lista como root:
// - mountIO (1 tela de folga): entrou na faixa → monta. A folga é pra bolha já
//   estar pronta quando aparecer.
// - unmountIO (2,5 telas): saiu da faixa → desmonta. A diferença entre as duas
//   faixas é histerese: rolar pra lá e pra cá na borda não fica remontando.
// - ResizeObserver: mede cada slot e a própria lista. Roda depois do layout e
//   ANTES do paint, então as correções de scroll daqui não piscam.
//
// Âncora de scroll feita à mão: WebKit (o webview do Tauri no macOS) não tem
// `overflow-anchor`, e a lista usa `overflow-anchor: none` pra que um webview
// que tenha não corrija em dobro. Quando uma bolha ACIMA da tela muda de altura
// (a estimativa virou altura real), o scrollTop anda o mesmo tanto e o que o
// usuário está lendo não pula.

export type SlotSetter = (mounted: boolean, height: number) => void;

/** Altura do slot de uma bolha que nunca foi medida (histórico recém-aberto). */
export const ESTIMATED_SLOT_HEIGHT = 120;

const MOUNT_MARGIN = '100% 0px';
const UNMOUNT_MARGIN = '250% 0px';

/** Mesmo limiar de antes: a menos disso do fim, a lista segue as mensagens novas. */
const PIN_THRESHOLD_PX = 80;

interface Slot {
  id: string;
  set: SlotSetter;
}

export class MessageVirtualizer {
  private container: HTMLElement | null = null;
  private mountIO: IntersectionObserver | null = null;
  private unmountIO: IntersectionObserver | null = null;
  private resizeObs: ResizeObserver | null = null;
  private slots = new Map<Element, Slot>();
  private heights = new Map<string, number>();
  private pinned = true;

  /** Liga os observadores na lista. Os slots podem registrar antes disso
   * (efeito de filho roda antes do efeito do pai) — são observados aqui. */
  attach(container: HTMLElement): () => void {
    this.container = container;
    this.mountIO = new IntersectionObserver(this.onMountZone, {
      root: container,
      rootMargin: MOUNT_MARGIN,
    });
    this.unmountIO = new IntersectionObserver(this.onUnmountZone, {
      root: container,
      rootMargin: UNMOUNT_MARGIN,
    });
    this.resizeObs = new ResizeObserver(this.onResize);
    this.resizeObs.observe(container);
    for (const el of this.slots.keys()) this.observe(el);

    return () => {
      this.mountIO?.disconnect();
      this.unmountIO?.disconnect();
      this.resizeObs?.disconnect();
      this.mountIO = this.unmountIO = this.resizeObs = null;
      this.container = null;
    };
  }

  register(el: Element, id: string, set: SlotSetter): () => void {
    this.slots.set(el, { id, set });
    this.observe(el);
    return () => {
      this.slots.delete(el);
      this.heights.delete(id);
      this.mountIO?.unobserve(el);
      this.unmountIO?.unobserve(el);
      this.resizeObs?.unobserve(el);
    };
  }

  handleScroll = () => {
    const el = this.container;
    if (!el) return;
    this.pinned = el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD_PX;
  };

  /** Mensagem nova / indicador de digitando: se o usuário está no fim, segue. */
  followIfPinned() {
    const el = this.container;
    if (this.pinned && el) el.scrollTop = el.scrollHeight;
  }

  /** Outra conversa entrou na lista (sessão aberta pelo sidebar): começa no fim. */
  pinToBottom() {
    this.pinned = true;
    this.followIfPinned();
  }

  private observe(el: Element) {
    this.mountIO?.observe(el);
    this.unmountIO?.observe(el);
    this.resizeObs?.observe(el);
  }

  private onMountZone = (entries: IntersectionObserverEntry[]) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const slot = this.slots.get(entry.target);
      slot?.set(true, 0);
    }
  };

  private onUnmountZone = (entries: IntersectionObserverEntry[]) => {
    for (const entry of entries) {
      if (entry.isIntersecting) continue;
      const slot = this.slots.get(entry.target);
      if (!slot) continue;
      // O ResizeObserver roda antes da entrega deste callback no mesmo frame,
      // então a altura guardada é a da bolha como ela está agora.
      slot.set(false, this.heights.get(slot.id) ?? ESTIMATED_SLOT_HEIGHT);
    }
  };

  private onResize = (entries: ResizeObserverEntry[]) => {
    const container = this.container;
    if (!container) return;

    const changed: { el: HTMLElement; prev: number; delta: number }[] = [];
    for (const entry of entries) {
      const slot = this.slots.get(entry.target);
      if (!slot) continue; // a própria lista mudou de tamanho: só o pin abaixo
      const el = entry.target as HTMLElement;
      const height = entry.borderBoxSize?.[0]?.blockSize ?? el.offsetHeight;
      const prev = this.heights.get(slot.id);
      this.heights.set(slot.id, height);
      if (prev !== undefined && prev !== height) {
        changed.push({ el, prev, delta: height - prev });
      }
    }

    if (this.pinned) {
      container.scrollTop = container.scrollHeight;
      return;
    }
    if (changed.length === 0) return;

    // Só compensa quem estava INTEIRO acima da tela antes da mudança. O
    // offsetTop já é o do layout novo, então desconta o quanto os slots mudados
    // acima dele o empurraram (por isso a ordem do documento).
    changed.sort((a, b) => a.el.offsetTop - b.el.offsetTop);
    const viewTop = container.scrollTop;
    let shift = 0;
    let correction = 0;
    for (const { el, prev, delta } of changed) {
      const topBefore = el.offsetTop - shift;
      if (topBefore + prev <= viewTop) correction += delta;
      shift += delta;
    }
    if (correction !== 0) container.scrollTop = viewTop + correction;
  };
}
