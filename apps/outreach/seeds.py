"""Default e-mail template guidance (editable in the UI). These are writing instructions, not data."""
from .models import EmailTemplate, TemplateKind

COMMON_RULES = (
    "Rules: international business English; short paragraphs; no hype words; no invented facts, "
    "numbers, customers, certifications or capacities; mention only approved company facts; "
    "reference one or two specific, sourced facts about the buyer to show the e-mail is not generic; "
    "never state prices, discounts, payment terms or delivery times; end with one clear question."
)

TEMPLATES = [
    {
        "kind": TemplateKind.OEM_PROPOSAL, "name": "OEM manufacturing proposal",
        "purpose": "Propose Ajay Plastic as an OEM manufacturing partner to a company that sells valves or irrigation products under its own brand.",
        "guidance": "1) Open with the buyer's own brand/product line you found (cite it). 2) One sentence on why an OEM partner in India could fit "
                    "(only approved capabilities). 3) Invite them to share the specifications, sizes and annual quantities they currently source. "
                    "4) Offer the catalogue and technical data. " + COMMON_RULES,
        "example_subject": "OEM supply of plastic ball valves for {brand}",
        "call_to_action": "Ask which valve types and sizes they currently outsource and their typical annual volumes.",
        "auto": True,
    },
    {
        "kind": TemplateKind.PRIVATE_LABEL, "name": "Private-label manufacturing",
        "purpose": "Offer private-label (their brand, our manufacturing) to distributors/retail brands that resell valves.",
        "guidance": "1) Reference their current range or brand. 2) Explain private-label options that appear in the approved OEM services list only. "
                    "3) Ask what branding, packaging and sizes they would need. " + COMMON_RULES,
        "example_subject": "Private-label PVC/PP valves for {company}",
        "call_to_action": "Ask which products they would like under their own label and the packaging they use.",
        "auto": True,
    },
    {
        "kind": TemplateKind.DISTRIBUTOR, "name": "Distributor appointment",
        "purpose": "Introduce Flotec products to a distributor/wholesaler and explore distribution of the Flotec brand in their market.",
        "guidance": "1) Reference their market segment and product range. 2) Introduce the relevant Flotec categories (verified products only). "
                    "3) Ask whether they would consider adding a plastic valve line and what they need from a supplier. "
                    "Do NOT offer exclusivity or territory rights. " + COMMON_RULES,
        "example_subject": "Plastic valves for your {segment} range",
        "call_to_action": "Ask if they would review the catalogue and share the product groups they buy most.",
        "auto": True,
    },
    {
        "kind": TemplateKind.IMPORTER, "name": "Importer introduction",
        "purpose": "Introduce Ajay Plastic to an importer that brings plumbing/irrigation products into its country.",
        "guidance": "1) Mention what they import (sourced). 2) Briefly introduce the manufacturer and relevant categories. "
                    "3) Ask about the products and specifications they import and the standards required in their market. " + COMMON_RULES,
        "example_subject": "Manufacturer of PVC / PP valves – Ahmedabad, India",
        "call_to_action": "Ask which products and standards they need and whether a catalogue would help.",
        "auto": True,
    },
    {
        "kind": TemplateKind.FIRST_EXPORT, "name": "First export introduction",
        "purpose": "General first introduction to a relevant buyer when no more specific template fits.",
        "guidance": "1) One line on who we are (approved facts). 2) One line connecting to what the buyer does (sourced). "
                    "3) Ask about their current requirements. Keep under 130 words. " + COMMON_RULES,
        "example_subject": "Plastic valves and irrigation fittings from India",
        "call_to_action": "Ask whether plastic valves or drip fittings are part of what they source.",
        "auto": True,
    },
    {
        "kind": TemplateKind.PRODUCT_SPECIFIC, "name": "Product-specific introduction",
        "purpose": "Introduce one specific verified product category that matches what the buyer sells.",
        "guidance": "1) Name the buyer's matching product line (sourced). 2) Describe the matching Flotec product using only verified "
                    "product data (sizes, material, connection types as listed). 3) Ask for their sizes and quantities. "
                    "Technical claims beyond the verified data are forbidden. " + COMMON_RULES,
        "example_subject": "{product} for {company}",
        "call_to_action": "Ask which sizes and connection types they use and the quantity per order.",
        "auto": True,
    },
    {
        "kind": TemplateKind.EXHIBITION, "name": "Exhibition follow-up",
        "purpose": "Follow up a contact met (or listed) at a trade exhibition. Use only when the CRM records the exhibition.",
        "guidance": "1) Reference the exhibition recorded in the CRM notes - never invent a meeting. 2) Recap what was discussed only if recorded. "
                    "3) Offer the catalogue and ask for next steps. " + COMMON_RULES,
        "example_subject": "Following up after {exhibition}",
        "call_to_action": "Ask for a convenient time for a short call or their current requirements.",
        "auto": False,
    },
    {
        "kind": TemplateKind.EXISTING_CUSTOMER, "name": "Existing customer follow-up",
        "purpose": "Stay in touch with an existing customer about repeat orders or additional product lines.",
        "guidance": "1) Reference the last recorded order/interaction from the CRM. 2) Ask about upcoming requirements or other products they need. "
                    "Never mention prices or delivery dates. " + COMMON_RULES,
        "example_subject": "Your upcoming valve requirements",
        "call_to_action": "Ask about requirements for the coming months.",
        "auto": False,
    },
    {
        "kind": TemplateKind.QUOTATION_FOLLOWUP, "name": "Quotation follow-up",
        "purpose": "Follow up on a quotation that was sent and approved by a manager.",
        "guidance": "1) Reference the quotation number and date. 2) Ask whether they have questions or need changes to sizes or quantities. "
                    "Do NOT restate or change prices or terms. Always requires human approval. " + COMMON_RULES,
        "example_subject": "Quotation {number} – any questions?",
        "call_to_action": "Ask if the quotation meets their requirements or needs adjustment.",
        "auto": False,
    },
    {
        "kind": TemplateKind.NEGOTIATION, "name": "Negotiation and price discussion",
        "purpose": "Draft a response during a price negotiation for a manager to complete and approve.",
        "guidance": "Acknowledge the buyer's points politely and leave clear [MANAGER TO CONFIRM] placeholders for any price, discount, "
                    "payment term or delivery commitment. Never propose numbers yourself. Always requires manager approval. " + COMMON_RULES,
        "example_subject": "Re: {subject}",
        "call_to_action": "Propose a next step (call or revised quotation) without committing to terms.",
        "auto": False,
    },
]


def seed_templates() -> int:
    created = 0
    for t in TEMPLATES:
        _, was_created = EmailTemplate.objects.get_or_create(
            kind=t["kind"],
            defaults={
                "name": t["name"], "purpose": t["purpose"], "guidance": t["guidance"],
                "example_subject": t["example_subject"], "call_to_action": t["call_to_action"],
                # The owner must explicitly approve templates for Mode-3 auto-sending.
                "approved_for_auto_send": False,
            },
        )
        created += was_created
    return created
