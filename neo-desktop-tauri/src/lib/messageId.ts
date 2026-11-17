/**
 * Id único por bolha de chat.
 *
 * `Date.now().toString()` puro colide: dois eventos do mesmo turno que chegam no
 * mesmo milissegundo (tipicamente `thinking` + `command_request`) ganhavam o MESMO
 * id, e aí o `messages.find(m => m.id === messageId)` do handler de aprovação
 * achava a bolha errada (a de thinking, sem `.command`) e saía calado — o botão
 * travava em "Aguardando..." e o neo-core ficava esperando a aprovação até o
 * timeout de 300s. Sessão b19c0497: thinking e command ambos com id 1785380431096.
 */
export const newMessageId = (): string =>
  `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
