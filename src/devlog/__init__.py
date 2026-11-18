"""The devlog channel agent (dv-fc9d): post ideas from finished work, a draft
with a picture, the owner's tap on the exact preview, then the channel.

Only bot code publishes (``drafts.publish_draft``, called from the preview's
callback handler); the agent's tools (``tools``) can file a draft, never post.
"""
