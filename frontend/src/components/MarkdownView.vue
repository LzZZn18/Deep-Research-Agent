<script setup>
import { computed } from 'vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

const props = defineProps({
  source: { type: String, default: '' },
});

const html = computed(() => DOMPurify.sanitize(marked.parse(props.source || '')));
</script>

<template>
  <div class="markdown" v-html="html"></div>
</template>
