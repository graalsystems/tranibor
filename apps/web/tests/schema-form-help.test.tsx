import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SchemaForm, docLink, type JsonSchema } from "@/components/schema-form";

/**
 * S86-05: a field of a plugin server's `config_schema` can say, in one line under it, what to put there
 * (`x-help`) and link to the documentation that explains it (`x-link`). A schema is a plugin's text: a link
 * that is not a plain `https:` URL is not drawn.
 */
const SCHEMA: JsonSchema = {
  type: "object",
  properties: {
    base_url: {
      type: "string",
      format: "uri",
      description: "The instance's address",
      "x-help": "The address you open in your browser, without a path.",
      "x-link": { href: "https://docs.example.com/instance-url", label: "Where to find it" },
    },
    token: { type: "string", "x-help": "An API token.", "x-link": "https://docs.example.com/tokens" },
    plain: { type: "string" },
  },
};

describe("x-help and x-link", () => {
  it("draws the help under the field and the link after it, both described by the control", () => {
    render(<SchemaForm schema={SCHEMA} value={{}} onChange={() => {}} />);
    const field = screen.getByLabelText(/base url/i);
    const help = screen.getByText("The address you open in your browser, without a path.");
    expect(help.tagName).toBe("P");
    const link = screen.getByRole("link", { name: "Where to find it" });
    expect(link).toHaveAttribute("href", "https://docs.example.com/instance-url");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    const described = (field.getAttribute("aria-describedby") ?? "").split(" ");
    expect(described).toEqual(expect.arrayContaining([help.id, link.id]));
    expect(screen.getByText("The instance's address")).toBeInTheDocument(); // the description stays
  });

  it("a link given as a string reads `Documentation`", () => {
    render(<SchemaForm schema={SCHEMA} value={{}} onChange={() => {}} />);
    expect(screen.getByRole("link", { name: "Documentation" })).toHaveAttribute("href", "https://docs.example.com/tokens");
  });

  it("a field with neither draws neither", () => {
    render(<SchemaForm schema={SCHEMA} value={{}} onChange={() => {}} />);
    expect(screen.getByLabelText(/plain/i)).not.toHaveAttribute("aria-describedby");
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("draws no link whose scheme is not https", () => {
    expect(docLink("javascript:alert(1)")).toBeUndefined();
    expect(docLink({ href: "http://docs.example.com" })).toBeUndefined();
    expect(docLink({ href: "data:text/html,x", label: "x" })).toBeUndefined();
    expect(docLink(undefined)).toBeUndefined();
    expect(docLink({ href: "https://docs.example.com/a b" })).toBeUndefined();
    expect(docLink({ href: "https://docs.example.com/a", label: " " })).toEqual({ href: "https://docs.example.com/a", label: "Documentation" });
  });
});
