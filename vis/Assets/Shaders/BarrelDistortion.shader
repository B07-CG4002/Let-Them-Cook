Shader "Hidden/BarrelDistortion"
{
    SubShader
    {
        Tags { "RenderType" = "Opaque" "RenderPipeline" = "UniversalPipeline" }
        Cull Off ZWrite Off ZTest Always

        Pass
        {
            Name "BarrelDistortion"

            HLSLPROGRAM
            #pragma vertex Vert
            #pragma fragment Frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"
            #include "Packages/com.unity.render-pipelines.core/Runtime/Utilities/Blit.hlsl"

            float _K1;
            float _K2;

            half4 Frag(Varyings input) : SV_Target
            {
                float2 uv = input.texcoord;
                float2 centered = uv - 0.5;
                float r2 = dot(centered, centered);

                // Pre-warp outward so the lens's own pincushion magnification
                // cancels out and the image looks undistorted through the lens.
                float distortionFactor = 1.0 + _K1 * r2 + _K2 * r2 * r2;
                float2 warpedUV = 0.5 + centered * distortionFactor;

                if (warpedUV.x < 0.0 || warpedUV.x > 1.0 || warpedUV.y < 0.0 || warpedUV.y > 1.0)
                    return half4(0, 0, 0, 1);

                return SAMPLE_TEXTURE2D_X(_BlitTexture, sampler_LinearClamp, warpedUV);
            }
            ENDHLSL
        }
    }
}
