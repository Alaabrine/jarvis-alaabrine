import ReactMarkdown from "react-markdown";

/** Render chat content: Markdown for assistant replies, plain text for the user. */
export function MessageContent({
  role,
  content,
}: {
  role: "user" | "assistant";
  content: string;
}) {
  if (role === "user") {
    return <div className="bubble-body">{content}</div>;
  }
  return (
    <div className="bubble-body markdown">
      <ReactMarkdown
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
