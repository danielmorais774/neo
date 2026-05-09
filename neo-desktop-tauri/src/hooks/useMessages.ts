import { useState, useCallback } from 'react';
import type { Message } from '../types';

export const useMessages = () => {
  const [messages, setMessages] = useState<Message[]>([]);

  const addMessage = useCallback((msg: Omit<Message, 'id' | 'timestamp'>) => {
    const newMsg: Message = {
      ...msg,
      id: `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
      timestamp: new Date()
    };
    setMessages(prev => [...prev, newMsg]);
    return newMsg.id;
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  const loadMessages = useCallback((loadedMessages: any[]) => {
    setMessages(loadedMessages.map((msg: any) => ({
      ...msg,
      messageType: msg.messageType || 'text',
      timestamp: new Date(msg.timestamp)
    })));
  }, []);

  return {
    messages,
    setMessages,
    addMessage,
    clearMessages,
    loadMessages
  };
};
