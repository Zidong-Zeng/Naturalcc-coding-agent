"""B5 Memory Module - Advanced Version with keyword search, vector search, auto-summarization, and conflict management."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from common.io_utils import append_jsonl, read_json, read_text, read_yaml, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file


# ==================== Configuration & Paths ====================

def _memory_paths(config_path: str | Path) -> dict[str, Path | int]:
    """Parse memory configuration and return resolved paths."""
    path = Path(config_path).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict) or not isinstance(config.get("memory"), dict):
        raise ValueError("memory.yaml must define a memory object")
    memory = config["memory"]
    required = ["root_dir", "global_memory_dir", "conversation_memory_dir", "index_path", "max_memory_chars"]
    missing = [name for name in required if name not in memory]
    if missing:
        raise ValueError(f"memory.yaml missing: {', '.join(missing)}")
    root = resolve_from_file(memory["root_dir"], path)
    max_chars = memory["max_memory_chars"]
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_memory_chars must be a positive integer")
    return {
        "root": root,
        "global": root / memory["global_memory_dir"],
        "conversations": root / memory["conversation_memory_dir"],
        "index": root / memory["index_path"],
        "vectors": root / "vectors.json",
        "max_chars": max_chars,
    }


def _read_index(index_path: Path) -> dict:
    """Read memory index file."""
    if not index_path.exists():
        return {}
    index = read_json(index_path)
    if not isinstance(index, dict):
        raise ValueError("memory_index.json must be an object")
    return index


def _read_vectors(vectors_path: Path) -> dict:
    """Read vector embeddings file."""
    if not vectors_path.exists():
        return {}
    vectors = read_json(vectors_path)
    if not isinstance(vectors, dict):
        raise ValueError("vectors.json must be an object")
    return vectors


# ==================== Text Processing & Summarization ====================

def _extract_keywords(text: str) -> list[str]:
    """Extract keywords from text (Chinese and English words)."""
    text = text.lower()
    
    keywords = []
    
    # 按非字母数字中文字符分割
    for token in re.split(r'[^a-zA-Z0-9\u4e00-\u9fff]+', text):
        if not token:
            continue
        # 纯英文单词（3字母以上）
        if re.match(r'^[a-zA-Z]+$', token) and len(token) >= 3:
            keywords.append(token)
        # 纯中文（2字以上）
        elif re.match(r'^[\u4e00-\u9fff]+$', token) and len(token) >= 2:
            keywords.append(token)
        # 中英文混合
        else:
            eng_parts = re.findall(r'[a-zA-Z]{3,}', token)
            keywords.extend(eng_parts)
            cn_parts = re.findall(r'[\u4e00-\u9fff]{2,}', token)
            keywords.extend(cn_parts)
    
    # 过滤停用词
    stop_words = {'the', 'and', 'for', 'are', 'with', 'they', 'have', 'this', 'that', 'from', 'been', 'will', 'would', 'there', 'their', 'what', 'about', 'when', 'where', 'which', 'while', 'some', 'these', 'them', 'than', 'then', 'also', 'after', 'back', 'other', 'many', 'time', 'very', 'just', 'even', 'well', 'only', 'over', 'think', 'know', 'take', 'people', 'year', 'good', 'come', 'could', 'state', 'much', 'make', 'like', 'use', 'her', 'him', 'see', 'way', 'who', 'its', 'may', 'say', 'she', 'try', 'ask', 'end', 'why', 'let', 'put', 'own', 'too', 'old', 'tell', 'said', 'each', 'first', 'never', 'being', 'every', 'great', 'might', 'shall', 'still', 'those', 'into', 'down', 'most', 'long', 'last', 'find', 'give', 'does', 'made', 'part', 'such', 'keep', 'call', 'came', 'need', 'feel', 'seem', 'turn', 'hand', 'sure', 'upon', 'head', 'help', 'home', 'side', 'move', 'both', 'five', 'once', 'same', 'must', 'name', 'left', 'done', 'open', 'case', 'show', 'live', 'play', 'went', 'told', 'seen', 'hear', 'talk', 'soon', 'read', 'stop', 'face', 'fact', 'land', 'line', 'kind', 'next'}
    
    filtered = [w for w in keywords if w not in stop_words]
    return filtered

def _keyword_score(query: str, content: str) -> float:
    """Calculate keyword matching score between query and content."""
    query_keywords = set(_extract_keywords(query))
    content_keywords = set(_extract_keywords(content))
    if not query_keywords:
        return 0.0
    intersection = query_keywords & content_keywords
    return len(intersection) / len(query_keywords)


def _call_b4_for_summary(text: str, max_length: int = 300, model_config_path: str | None = None,
                          timeout: float = 15.0) -> str:
    """Call B4 LLM to generate a summary for long text.

    路径解析优先级:
    1. 显式传入 model_config_path
    2. 自动定位：本文件所在目录(code/) → ../configs/model.yaml (agent/configs/model.yaml)
    找不到 / 超时 / 失败时返回空串触发 fallback
    """
    import concurrent.futures

    try:
        # 自动定位 model_config（解决 CLI 调用 vs Web 调用 cwd 不同的问题）
        if model_config_path is None:
            auto_path = Path(__file__).resolve().parent / ".." / "configs" / "model.yaml"
            if not auto_path.is_file():
                return ""   # 找不到 → 触发 fallback
            model_config_path = str(auto_path)

        # Import B4's LLM generation function
        from b4_local_agent_llm import generate_ai_message

        # Prepare summary prompt
        prompt_messages = [
            {
                "role": "system",
                "content": "你是一个专业的文本摘要助手。请将用户提供的长文本总结为简洁的摘要，保留核心信息。"
            },
            {
                "role": "user",
                "content": f"请将以下文本总结为{max_length}字以内的摘要：\n\n{text[:2000]}"
            }
        ]

        # Call B4 in a thread with timeout — 防止 LLM API 阻塞整个保存流程
        def _generate():
            return generate_ai_message(
                model_config=model_config_path,
                messages=prompt_messages,
                tools_schema=[],
                mode="prompt_json",
                artifact_dir=str(Path(__file__).resolve().parent / ".." / "outputs" / "b5_summary"),
                artifact_stem="b5_summary",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_generate)
            try:
                result = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                print(f"Warning: B4 summary generation timed out after {timeout}s, falling back")
                future.cancel()
                return ""

        # Extract summary from result
        if result.get("status") == "success":
            ai_message = result.get("ai_message", {})
            summary = ai_message.get("content", "").strip()
            if summary:
                return summary[:max_length]
    except Exception as e:
        print(f"Warning: B4 summary generation failed: {e}, falling back to simple summarization")

    # Fallback to simple summarization if B4 fails
    return _simple_summarize(text, max_length)


def _simple_summarize(text: str, max_length: int = 300) -> str:
    """Generate a simple extractive summary of text (fallback method)."""
    sentences = re.split(r'(?<=[。！？.!?])\s*', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    
    if not sentences:
        return text[:max_length] + "..." if len(text) > max_length else text
    
    # Score sentences by keyword frequency
    word_freq = {}
    for sentence in sentences:
        for word in _extract_keywords(sentence):
            word_freq[word] = word_freq.get(word, 0) + 1
    
    sentence_scores = []
    for sentence in sentences:
        score = sum(word_freq.get(word, 0) for word in _extract_keywords(sentence))
        sentence_scores.append((sentence, score))
    
    # Sort by score and select top sentences
    sentence_scores.sort(key=lambda x: x[1], reverse=True)
    
    summary = ""
    for sentence, _ in sentence_scores:
        if len(summary) + len(sentence) <= max_length:
            summary += sentence + " "
        else:
            break
    
    return summary.strip() or text[:max_length]


def _compress_messages(messages: list[dict], max_chars: int = 1000) -> str:
    """Compress messages list into a summary."""
    if not messages:
        return ""
    
    # Extract key information from messages
    content_parts = []
    for msg in messages[-5:]:  # Last 5 messages
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            content_parts.append(f"{role}: {content[:200]}")
    
    full_text = " | ".join(content_parts)
    if len(full_text) > max_chars:
        return _simple_summarize(full_text, max_chars)
    return full_text


# ==================== Vector Operations ====================

_transformer_model = None

def _get_transformer_model():
    """Skip sentence-transformers model loading, use pure Python vectorization."""
    global _transformer_model
    _transformer_model = None  # 强制使用纯Python向量，不加载任何模型
    return _transformer_model


def _text_to_vector(text: str, dim: int = 384) -> list[float]:
    """Convert text to a vector using TF-IDF weighted word frequency (no model download needed)."""
    import math
    import re
    import hashlib
    
    # 分词：中文按字，英文按词
    text = text.lower()
    
    # 提取中文单字和英文单词
    tokens = []
    # 中文单字
    cn_chars = re.findall(r'[\u4e00-\u9fff]', text)
    tokens.extend(cn_chars)
    # 英文单词（3字母以上）
    en_words = re.findall(r'[a-z]{3,}', text)
    tokens.extend(en_words)
    
    if not tokens:
        # 如果没有提取到词，回退到字符n-gram
        tokens = [text[i:i+2] for i in range(len(text)-1)]
    
    # 计算词频
    word_freq = {}
    for token in tokens:
        word_freq[token] = word_freq.get(token, 0) + 1
    
    # TF-IDF 风格的加权：稀有词权重更高
    total_tokens = len(tokens)
    vector = [0.0] * dim
    
    for word, freq in word_freq.items():
        # TF = 词频 / 总词数
        tf = freq / total_tokens
        # 用 hash 映射到维度
        idx = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16) % dim
        # 加权：词频越高、词越短（可能是更核心的词）权重越高
        weight = tf * (1.0 / (1 + math.log(1 + len(word))))
        vector[idx] += weight
    
    # L2 归一化
    magnitude = sum(x**2 for x in vector) ** 0.5
    if magnitude > 0:
        vector = [x / magnitude for x in vector]
    
    return vector


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm1 = sum(a ** 2 for a in v1) ** 0.5
    norm2 = sum(b ** 2 for b in v2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot_product / (norm1 * norm2)


def _update_vectors(config_path: str, memory_id: str, content: str) -> None:
    """Update vector embeddings for a memory document."""
    paths = _memory_paths(config_path)
    vectors = _read_vectors(paths["vectors"])
    vectors[memory_id] = _text_to_vector(content)
    write_json(vectors, paths["vectors"])


# ==================== Conflict Detection & Resolution ====================

def _detect_conflicts(old_content: str, new_content: str) -> dict:
    """Detect conflicts between old and new memory content."""
    # Extract key facts (sentences) from both versions
    old_sentences = set(re.split(r'(?<=[。！？.!?])\s*', old_content))
    new_sentences = set(re.split(r'(?<=[。！？.!?])\s*', new_content))
    
    old_sentences = {s.strip() for s in old_sentences if len(s.strip()) > 10}
    new_sentences = {s.strip() for s in new_sentences if len(s.strip()) > 10}
    
    # Find similar sentences (potential conflicts)
    conflicts = []
    supplementary = []
    identical = []
    
    for new_sent in new_sentences:
        best_match = None
        best_ratio = 0.0
        
        for old_sent in old_sentences:
            ratio = SequenceMatcher(None, new_sent, old_sent).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = old_sent
        
        if best_ratio > 0.8:
            identical.append({"old": best_match, "new": new_sent, "similarity": best_ratio})
        elif best_ratio > 0.5:
            conflicts.append({"old": best_match, "new": new_sent, "similarity": best_ratio})
        else:
            supplementary.append(new_sent)
    
    return {
        "conflicts": conflicts,
        "supplementary": supplementary,
        "identical": identical,
        "removed": list(old_sentences - new_sentences)
    }


def _parse_markdown_structure(content: str) -> dict:
    """Parse markdown content into structured sections."""
    lines = content.split('\n')
    structure = {
        "title": "",
        "metadata": {},
        "sections": []
    }
    current_section = None
    
    for line in lines:
        stripped = line.strip()
        
        # Title
        if stripped.startswith('# '):
            structure["title"] = stripped[2:]
            continue
        
        # Metadata
        if stripped.startswith('- ') and ':' in stripped:
            key_val = stripped[2:].split(':', 1)
            if len(key_val) == 2:
                key = key_val[0].strip()
                val = key_val[1].strip().strip('`')
                structure["metadata"][key] = val
            continue
        
        # Section header
        if stripped.startswith('## '):
            if current_section:
                structure["sections"].append(current_section)
            current_section = {"header": stripped[3:], "content": []}
            continue
        
        # Skip code blocks
        if stripped.startswith('```'):
            continue
        
        # Content
        if current_section is not None:
            if stripped:
                current_section["content"].append(stripped)
            else:
                if current_section["content"]:
                    current_section["content"].append("")
    
    if current_section:
        structure["sections"].append(current_section)
    
    return structure


def _reconstruct_markdown(structure: dict) -> str:
    """Reconstruct markdown from structured sections."""
    lines = []
    
    if structure["title"]:
        lines.append(f"# {structure['title']}")
        lines.append("")
    
    for key, val in structure["metadata"].items():
        lines.append(f"- {key}: `{val}`")
    
    if structure["metadata"]:
        lines.append("")
    
    for section in structure["sections"]:
        lines.append(f"## {section['header']}")
        lines.append("")
        for line in section["content"]:
            lines.append(line)
        lines.append("")
    
    return '\n'.join(lines).rstrip()


def _merge_contents(old_content: str, new_content: str, strategy: str = "smart") -> str:
    """Merge old and new content with conflict resolution while preserving markdown structure."""
    if strategy == "replace":
        return new_content
    elif strategy == "append":
        return old_content + "\n\n---\n\n" + new_content
    elif strategy == "smart":
        if not old_content:
            return new_content
        
        # Parse both documents
        old_struct = _parse_markdown_structure(old_content)
        new_struct = _parse_markdown_structure(new_content)
        
        # Merge by sections
        old_sections = {s["header"]: s for s in old_struct["sections"]}
        merged_sections = []
        seen_headers = set()
        
        for new_sec in new_struct["sections"]:
            header = new_sec["header"]
            seen_headers.add(header)
            
            if header in old_sections:
                old_sec = old_sections[header]
                old_text = '\n'.join(old_sec["content"]).strip()
                new_text = '\n'.join(new_sec["content"]).strip()
                
                if not old_text:
                    merged_sections.append(new_sec)
                    continue
                if not new_text:
                    merged_sections.append(old_sec)
                    continue
                
                # Calculate similarity
                ratio = SequenceMatcher(None, old_text, new_text).ratio()
                
                if ratio > 0.9:
                    # Nearly identical, use new version
                    merged_sections.append(new_sec)
                elif ratio > 0.6:
                    # Partial overlap, merge intelligently
                    old_lines_set = set(old_sec["content"])
                    new_lines_set = set(new_sec["content"])
                    common_lines = old_lines_set & new_lines_set
                    unique_old = old_lines_set - new_lines_set
                    unique_new = new_lines_set - old_lines_set
                    
                    combined_lines = []
                    for line in old_sec["content"]:
                        if line in common_lines:
                            combined_lines.append(line)
                    
                    if unique_old:
                        combined_lines.append("")
                        combined_lines.append("**[保留内容]**")
                        for line in unique_old:
                            combined_lines.append(line)
                    
                    if unique_new:
                        combined_lines.append("")
                        combined_lines.append("**[新增内容]**")
                        for line in unique_new:
                            combined_lines.append(line)
                    
                    merged_sections.append({"header": header, "content": combined_lines})
                else:
                    # Low similarity, keep both with markers
                    if len(old_text) > len(new_text) * 2:
                        combined_content = old_sec["content"] + ["", "**[补充内容]**"] + new_sec["content"]
                    else:
                        combined_content = new_sec["content"] + ["", "**[历史内容]**"] + old_sec["content"]
                    merged_sections.append({"header": header, "content": combined_content})
            else:
                # New section
                merged_sections.append(new_sec)
        
        # Add old sections that weren't in new content
        for header, old_sec in old_sections.items():
            if header not in seen_headers:
                merged_sections.append(old_sec)
        
        # Reconstruct document
        merged_struct = {
            "title": new_struct["title"] or old_struct["title"],
            "metadata": {**old_struct["metadata"], **new_struct["metadata"]},
            "sections": merged_sections
        }
        
        return _reconstruct_markdown(merged_struct)
    
    return new_content


# ==================== Error Impact Analysis ====================

def _analyze_error_impact(memory_content: str, final_answer: str) -> dict:
    """Analyze how errors in memory might affect the final answer."""
    # Extract potential facts from memory
    memory_facts = re.findall(r'[^。！？.!?]+[。！？.!?]', memory_content)
    
    # Check for contradictions between memory and answer
    contradictions = []
    unsupported = []
    
    for fact in memory_facts[:10]:  # Check first 10 facts
        fact_keywords = set(_extract_keywords(fact))
        answer_keywords = set(_extract_keywords(final_answer))
        
        # If fact keywords are not in answer, it might be unsupported
        if fact_keywords and not (fact_keywords & answer_keywords):
            unsupported.append(fact.strip())
    
    # Calculate reliability score
    total_facts = len(memory_facts)
    if total_facts > 0:
        unsupported_ratio = len(unsupported) / total_facts
        reliability_score = max(0, 1 - unsupported_ratio)
    else:
        reliability_score = 1.0
    
    return {
        "reliability_score": round(reliability_score, 2),
        "total_facts": total_facts,
        "unsupported_facts": unsupported[:5],
        "potential_contradictions": contradictions,
        "risk_level": "high" if reliability_score < 0.5 else "medium" if reliability_score < 0.8 else "low"
    }


# ==================== Core Functions ====================

def search_memory_by_keywords(
    config_path: str,
    query: str,
    top_k: int = 5,
    memory_type: str | None = None,
    outdir: str | None = None,
) -> dict:
    """Search memories by keyword relevance and return top-k results."""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    
    results = []
    for memory_id, metadata in index.items():
        if memory_type and metadata.get("memory_type") != memory_type:
            continue
        
        relative_path = metadata.get("path")
        if not relative_path:
            continue
        
        document_path = paths["root"] / relative_path
        if not document_path.is_file():
            continue
        
        content = read_text(document_path)
        score = _keyword_score(query, content)
        
        if score > 0:
            results.append({
                "memory_id": memory_id,
                "memory_type": metadata.get("memory_type"),
                "title": metadata.get("title", memory_id),
                "path": relative_path,
                "relevance_score": round(score, 4),
                "content_preview": content[:500] + "..." if len(content) > 500 else content,
            })
    
    # Sort by relevance score
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    top_results = results[:top_k]
    
    result = {
        "status": "success",
        "query": query,
        "top_k": top_k,
        "total_matches": len(results),
        "results": top_results,
    }
    
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "keyword_search_results.json")
    
    return result


def search_memory_by_vector(
    config_path: str,
    query: str,
    top_k: int = 5,
    outdir: str | None = None,
) -> dict:
    """Search memories by vector similarity and return top-k results."""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    vectors = _read_vectors(paths["vectors"])
    
    query_vector = _text_to_vector(query)
    
    results = []
    for memory_id, metadata in index.items():
        relative_path = metadata.get("path")
        if not relative_path:
            continue
        
        # Get or compute vector
        if memory_id in vectors:
            mem_vector = vectors[memory_id]
        else:
            document_path = paths["root"] / relative_path
            if not document_path.is_file():
                continue
            content = read_text(document_path)
            mem_vector = _text_to_vector(content)
            vectors[memory_id] = mem_vector
        
        similarity = _cosine_similarity(query_vector, mem_vector)
        
        if similarity > 0:
            document_path = paths["root"] / relative_path
            content = read_text(document_path) if document_path.is_file() else ""
            results.append({
                "memory_id": memory_id,
                "memory_type": metadata.get("memory_type"),
                "title": metadata.get("title", memory_id),
                "path": relative_path,
                "similarity_score": round(similarity, 4),
                "content_preview": content[:500] + "..." if len(content) > 500 else content,
            })
    
    # Sort by similarity
    results.sort(key=lambda x: x["similarity_score"], reverse=True)
    top_results = results[:top_k]
    
    # Save updated vectors
    write_json(vectors, paths["vectors"])
    
    result = {
        "status": "success",
        "query": query,
        "top_k": top_k,
        "total_matches": len(results),
        "results": top_results,
    }
    
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "vector_search_results.json")
    
    return result


def update_memory(
    config_path: str,
    memory_id: str,
    new_messages: list[dict],
    new_answer: str,
    merge_strategy: str = "smart",
    outdir: str | None = None,
) -> dict:
    """Update an existing memory document with conflict detection and resolution."""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    
    if memory_id not in index:
        raise ValueError(f"Memory {memory_id} not found")
    
    metadata = index[memory_id]
    relative_path = metadata.get("path")
    document_path = paths["root"] / relative_path
    
    # Read old content
    old_content = read_text(document_path) if document_path.is_file() else ""
    
    # Generate new content
    now = now_iso()
    compressed_messages = _compress_messages(new_messages, 800)
    
    new_content = (
        f"# {metadata.get('title', memory_id)}\n\n"
        f"- memory_id: `{memory_id}`\n"
        f"- conversation_id: `{metadata.get('conversation_id', 'unknown')}`\n"
        f"- updated_at: `{now}`\n\n"
        f"## Final Answer\n\n{new_answer}\n\n"
        f"## Messages Summary\n\n{compressed_messages}\n"
    )
    
    # Detect conflicts
    conflict_analysis = _detect_conflicts(old_content, new_content)
    
    # Merge contents
    merged_content = _merge_contents(old_content, new_content, merge_strategy)
    
    # Write merged content
    write_text(merged_content, document_path)
    
    # Update index
    metadata["updated_at"] = now
    metadata["summary"] = new_answer[:200]
    index[memory_id] = metadata
    write_json(index, paths["index"])
    
    # Update vectors
    _update_vectors(config_path, memory_id, merged_content)
    
    result = {
        "status": "success",
        "memory_id": memory_id,
        "merge_strategy": merge_strategy,
        "conflict_analysis": conflict_analysis,
        "old_content_length": len(old_content),
        "new_content_length": len(new_content),
        "merged_content_length": len(merged_content),
        "updated_at": now,
    }
    
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "update_result.json")
    
    return result


def save_memory_advanced(
    config_path: str,
    conversation_id: str,
    save_type: str,
    messages: list[dict],
    trace: dict,
    answer: str,
    auto_summarize: bool = True,
    use_llm_summary: bool = True,
    outdir: str | None = None,
) -> dict:
    """Save memory with automatic summarization for long content.
    
    Args:
        config_path: Path to memory configuration
        conversation_id: Unique conversation identifier
        save_type: Type of memory ("conversation" or "global")
        messages: List of conversation messages
        trace: Execution trace information
        answer: Final answer text
        auto_summarize: Whether to enable auto-summarization
        use_llm_summary: Whether to use B4 LLM for summarization (fallback to simple method if False or failed)
        outdir: Output directory for result files
    """
    paths = _memory_paths(config_path)
    
    conversation_id = re.sub(r'[^A-Za-z0-9_.-]', '', conversation_id)
    if not conversation_id:
        raise ValueError("Invalid conversation_id")
    
    if save_type not in {"conversation", "global"}:
        raise ValueError("save_type must be conversation or global")
    
    now = now_iso()
    memory_id = f"mem_{save_type}_{conversation_id}"
    target_dir = paths["conversations"] if save_type == "conversation" else paths["global"]
    relative_dir = "conversations" if save_type == "conversation" else "global"
    target_path = Path(target_dir) / f"{conversation_id}.md"
    relative_path = f"{relative_dir}/{conversation_id}.md"
    
    # Generate title and summary
    title = f"{save_type.title()} {conversation_id}"
    
    # Use LLM for summary if content is long and use_llm_summary is True
    if len(answer) > 200 and use_llm_summary:
        summary = _call_b4_for_summary(answer, 200)
    else:
        summary = answer[:200] if len(answer) <= 200 else _simple_summarize(answer, 200)
    
    # Compress messages if too long
    messages_json = json.dumps(messages, ensure_ascii=False)
    if auto_summarize and len(messages_json) > 2000:
        if use_llm_summary:
            # Prepare messages text for LLM summarization
            messages_text = "\n".join([f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in messages[-5:]])
            messages_summary = _call_b4_for_summary(messages_text, 1000)
        else:
            messages_summary = _compress_messages(messages, 1000)
        messages_section = f"## Messages Summary\n\n{messages_summary}\n"
    else:
        messages_section = f"## Messages\n\n```json\n{messages_json}\n```\n"
    
    # Compress answer if too long
    if auto_summarize and len(answer) > 1000:
        if use_llm_summary:
            answer_summary = _call_b4_for_summary(answer, 500)
        else:
            answer_summary = _simple_summarize(answer, 500)
        answer_section = (
            f"## Final Answer\n\n{answer[:500]}\n\n"
            f"### Summary (LLM Generated)\n\n{answer_summary}\n"
        )
    else:
        answer_section = f"## Final Answer\n\n{answer}\n"
    
    markdown = (
        f"# {title}\n\n"
        f"- memory_id: `{memory_id}`\n"
        f"- conversation_id: `{conversation_id}`\n"
        f"- created_or_updated_at: `{now}`\n\n"
        f"{answer_section}\n"
        f"{messages_section}\n"
        f"## Trace\n\n```json\n{json.dumps(trace, ensure_ascii=False, indent=2)}\n```\n"
    )
    
    write_text(markdown, target_path)
    
    # Update index
    index = _read_index(paths["index"])
    existing = index.get(memory_id, {})
    created_at = existing.get("created_at", now)
    index[memory_id] = {
        "memory_id": memory_id,
        "memory_type": save_type,
        "title": title,
        "summary": summary,
        "path": relative_path,
        "conversation_id": conversation_id,
        "created_at": created_at,
        "updated_at": now,
    }
    write_json(index, paths["index"])
    
    # Update vectors
    _update_vectors(config_path, memory_id, markdown)
    
    result = {
        "status": "success",
        "memory_id": memory_id,
        "memory_type": save_type,
        "conversation_id": conversation_id,
        "title": title,
        "summary": summary,
        "path": relative_path,
        "created_at": created_at,
        "updated_at": now,
        "auto_summarized": auto_summarize,
    }
    
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "saved_memory.json")
        append_jsonl(
            {"timestamp": now, "operation": "save_advanced", "status": "success", "memory_id": memory_id},
            output_dir / "memory_log.jsonl",
        )
    
    return result


def analyze_memory_errors(
    config_path: str,
    memory_id: str,
    final_answer: str,
    outdir: str | None = None,
) -> dict:
    """Analyze potential errors in memory and their impact on final answer."""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    
    if memory_id not in index:
        raise ValueError(f"Memory {memory_id} not found")
    
    metadata = index[memory_id]
    relative_path = metadata.get("path")
    document_path = paths["root"] / relative_path
    
    if not document_path.is_file():
        raise ValueError(f"Memory file not found: {relative_path}")
    
    memory_content = read_text(document_path)
    
    # Perform error impact analysis
    analysis = _analyze_error_impact(memory_content, final_answer)
    analysis["memory_id"] = memory_id
    analysis["memory_title"] = metadata.get("title", memory_id)
    analysis["analyzed_at"] = now_iso()
    
    if outdir:
        output_dir = Path(outdir)
        write_json(analysis, output_dir / "error_analysis.json")
    
    return analysis


# ==================== CLI Interface ====================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B5 Advanced Memory Module")
    parser.add_argument("--config", required=True, help="Path to memory.yaml config file")
    parser.add_argument("--outdir", required=True, help="Output directory")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Keyword search command
    kw_parser = subparsers.add_parser("keyword_search", help="Search memories by keywords")
    kw_parser.add_argument("--query", required=True, help="Search query")
    kw_parser.add_argument("--top_k", type=int, default=5, help="Number of top results")
    kw_parser.add_argument("--memory_type", choices=["conversation", "global"], help="Filter by memory type")
    
    # Vector search command
    vec_parser = subparsers.add_parser("vector_search", help="Search memories by vector similarity")
    vec_parser.add_argument("--query", required=True, help="Search query")
    vec_parser.add_argument("--top_k", type=int, default=5, help="Number of top results")
    
    # Save command
    save_parser = subparsers.add_parser("save", help="Save memory with auto-summarization")
    save_parser.add_argument("--conversation_id", required=True)
    save_parser.add_argument("--save_type", required=True, choices=["conversation", "global"])
    save_parser.add_argument("--messages_path", required=True)
    save_parser.add_argument("--trace_path", required=True)
    save_parser.add_argument("--answer_path", required=True)
    save_parser.add_argument("--no_summarize", action="store_true", help="Disable auto-summarization")
    
    # Update command
    update_parser = subparsers.add_parser("update", help="Update existing memory")
    update_parser.add_argument("--memory_id", required=True)
    update_parser.add_argument("--messages_path", required=True)
    update_parser.add_argument("--answer_path", required=True)
    update_parser.add_argument("--merge_strategy", default="smart", choices=["smart", "replace", "append"])
    
    # Analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze memory errors")
    analyze_parser.add_argument("--memory_id", required=True)
    analyze_parser.add_argument("--answer_path", required=True)
    
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    
    if not args.command:
        parser.print_help()
        return 1
    
    try:
        config_path = resolve_cli_path(args.config)
        outdir = resolve_cli_path(args.outdir)
        
        if args.command == "keyword_search":
            result = search_memory_by_keywords(
                str(config_path),
                args.query,
                args.top_k,
                args.memory_type,
                str(outdir),
            )
            print(f"Results saved to: {outdir / 'keyword_search_results.json'}")
            
        elif args.command == "vector_search":
            result = search_memory_by_vector(
                str(config_path),
                args.query,
                args.top_k,
                str(outdir),
            )
            print(f"Results saved to: {outdir / 'vector_search_results.json'}")
            
        elif args.command == "save":
            messages = read_json(args.messages_path)
            trace = read_json(args.trace_path)
            answer = read_text(args.answer_path).strip()
            
            result = save_memory_advanced(
                str(config_path),
                args.conversation_id,
                args.save_type,
                messages,
                trace,
                answer,
                not args.no_summarize,
                True,
                str(outdir),
            )
            print(f"Memory saved: {outdir / 'saved_memory.json'}")
            
        elif args.command == "update":
            messages = read_json(args.messages_path)
            answer = read_text(args.answer_path).strip()
            
            result = update_memory(
                str(config_path),
                args.memory_id,
                messages,
                answer,
                args.merge_strategy,
                str(outdir),
            )
            print(f"Update result: {outdir / 'update_result.json'}")
            
        elif args.command == "analyze":
            answer = read_text(args.answer_path).strip()
            
            result = analyze_memory_errors(
                str(config_path),
                args.memory_id,
                answer,
                str(outdir),
            )
            print(f"Analysis saved: {outdir / 'error_analysis.json'}")
        
        return 0
        
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
