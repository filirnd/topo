/*
 * Minimal native MPEG-TS player for Topo experiments.
 *
 * It reads an MPEG-TS stream from stdin, decodes video with FFmpeg and
 * displays it with SDL2. It does not re-encode the media. The cloud relay
 * transport is intentionally kept outside this proof of concept.
 */

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
#include <libavutil/imgutils.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>
#include <SDL2/SDL.h>

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void print_ffmpeg_error(const char *operation, int error_code) {
    char message[AV_ERROR_MAX_STRING_SIZE];
    av_strerror(error_code, message, sizeof(message));
    fprintf(stderr, "%s: %s\n", operation, message);
}

static int should_quit(void) {
    SDL_Event event;
    while (SDL_PollEvent(&event)) {
        if (event.type == SDL_QUIT) {
            return 1;
        }
        if (event.type == SDL_KEYDOWN && event.key.keysym.sym == SDLK_ESCAPE) {
            return 1;
        }
    }
    return 0;
}

static void render_frame(
    SDL_Renderer *renderer,
    SDL_Texture *texture,
    const AVFrame *frame,
    int video_width,
    int video_height
) {
    SDL_UpdateYUVTexture(
        texture,
        NULL,
        frame->data[0],
        frame->linesize[0],
        frame->data[1],
        frame->linesize[1],
        frame->data[2],
        frame->linesize[2]
    );

    int window_width = 0;
    int window_height = 0;
    SDL_GetRendererOutputSize(renderer, &window_width, &window_height);
    double scale_x = (double)window_width / (double)video_width;
    double scale_y = (double)window_height / (double)video_height;
    double scale = scale_x < scale_y ? scale_x : scale_y;
    SDL_Rect destination = {
        .w = (int)((double)video_width * scale),
        .h = (int)((double)video_height * scale),
    };
    destination.x = (window_width - destination.w) / 2;
    destination.y = (window_height - destination.h) / 2;

    SDL_SetRenderDrawColor(renderer, 0, 0, 0, 255);
    SDL_RenderClear(renderer);
    SDL_RenderCopy(renderer, texture, NULL, &destination);
    SDL_RenderPresent(renderer);
}

static int queue_audio_frame(
    SDL_AudioDeviceID audio_device,
    SwrContext *resampler,
    const AVFrame *frame
) {
    int output_samples = (int)av_rescale_rnd(
        swr_get_delay(resampler, frame->sample_rate) + frame->nb_samples,
        16000,
        frame->sample_rate,
        AV_ROUND_UP
    );
    uint8_t *output = NULL;
    int output_linesize = 0;
    int error = av_samples_alloc(
        &output,
        &output_linesize,
        1,
        output_samples,
        AV_SAMPLE_FMT_S16,
        0
    );
    if (error < 0) {
        print_ffmpeg_error("av_samples_alloc", error);
        return error;
    }
    int converted = swr_convert(
        resampler,
        &output,
        output_samples,
        (const uint8_t **)frame->extended_data,
        frame->nb_samples
    );
    if (converted >= 0) {
        SDL_QueueAudio(audio_device, output, (Uint32)(converted * 2));
        /* Keep a short queue so an input hiccup cannot accumulate latency. */
        if (SDL_GetQueuedAudioSize(audio_device) > 32000) {
            SDL_ClearQueuedAudio(audio_device);
        }
    }
    av_freep(&output);
    return converted < 0 ? converted : 0;
}

int main(int argc, char **argv) {
    int result = 1;
    int video_stream_index = -1;
    int audio_stream_index = -1;
    AVFormatContext *format = NULL;
    AVCodecContext *decoder = NULL;
    AVCodecContext *audio_decoder = NULL;
    AVPacket *packet = NULL;
    AVFrame *decoded = NULL;
    AVFrame *display_frame = NULL;
    struct SwsContext *scaler = NULL;
    SwrContext *resampler = NULL;
    SDL_Window *window = NULL;
    SDL_Renderer *renderer = NULL;
    SDL_Texture *texture = NULL;
    SDL_AudioDeviceID audio_device = 0;
    uint8_t *display_buffer = NULL;
    int display_buffer_size = 0;
    const char *window_title = "Topo native FFmpeg player";
    for (int index = 1; index + 1 < argc; ++index) {
        if (strcmp(argv[index], "--title") == 0) {
            window_title = argv[index + 1];
            break;
        }
    }

    const AVInputFormat *mpegts = av_find_input_format("mpegts");
    if (mpegts == NULL) {
        fprintf(stderr, "FFmpeg MPEG-TS demuxer is unavailable.\n");
        goto cleanup;
    }

    AVDictionary *input_options = NULL;
    av_dict_set(&input_options, "probesize", "32768", 0);
    av_dict_set(&input_options, "analyzeduration", "0", 0);
    av_dict_set(&input_options, "fflags", "nobuffer", 0);
    int error = avformat_open_input(&format, "pipe:0", mpegts, &input_options);
    av_dict_free(&input_options);
    if (error < 0) {
        print_ffmpeg_error("avformat_open_input", error);
        goto cleanup;
    }
    format->flags |= AVFMT_FLAG_NOBUFFER;

    error = avformat_find_stream_info(format, NULL);
    if (error < 0) {
        print_ffmpeg_error("avformat_find_stream_info", error);
        goto cleanup;
    }
    video_stream_index = av_find_best_stream(
        format,
        AVMEDIA_TYPE_VIDEO,
        -1,
        -1,
        NULL,
        0
    );
    if (video_stream_index < 0) {
        print_ffmpeg_error("av_find_best_stream", video_stream_index);
        goto cleanup;
    }

    AVStream *video_stream = format->streams[video_stream_index];
    const AVCodec *codec = avcodec_find_decoder(video_stream->codecpar->codec_id);
    if (codec == NULL) {
        fprintf(stderr, "No decoder available for the video stream.\n");
        goto cleanup;
    }
    decoder = avcodec_alloc_context3(codec);
    if (decoder == NULL) {
        fprintf(stderr, "Could not allocate the decoder context.\n");
        goto cleanup;
    }
    error = avcodec_parameters_to_context(decoder, video_stream->codecpar);
    if (error < 0) {
        print_ffmpeg_error("avcodec_parameters_to_context", error);
        goto cleanup;
    }
    decoder->flags |= AV_CODEC_FLAG_LOW_DELAY;
    error = avcodec_open2(decoder, codec, NULL);
    if (error < 0) {
        print_ffmpeg_error("avcodec_open2", error);
        goto cleanup;
    }

    audio_stream_index = av_find_best_stream(
        format,
        AVMEDIA_TYPE_AUDIO,
        -1,
        -1,
        NULL,
        0
    );
    if (audio_stream_index >= 0) {
        AVStream *audio_stream = format->streams[audio_stream_index];
        const AVCodec *audio_codec = avcodec_find_decoder(audio_stream->codecpar->codec_id);
        if (audio_codec != NULL) {
            audio_decoder = avcodec_alloc_context3(audio_codec);
            if (audio_decoder == NULL ||
                avcodec_parameters_to_context(audio_decoder, audio_stream->codecpar) < 0 ||
                avcodec_open2(audio_decoder, audio_codec, NULL) < 0) {
                avcodec_free_context(&audio_decoder);
                audio_stream_index = -1;
            }
        } else {
            audio_stream_index = -1;
        }
    }
    if (audio_stream_index >= 0) {
        AVChannelLayout input_layout = audio_decoder->ch_layout;
        if (input_layout.nb_channels == 0) {
            av_channel_layout_default(
                &input_layout,
                audio_decoder->ch_layout.nb_channels > 0 ? audio_decoder->ch_layout.nb_channels : 1
            );
        }
        const AVChannelLayout output_layout = AV_CHANNEL_LAYOUT_MONO;
        error = swr_alloc_set_opts2(
            &resampler,
            &output_layout,
            AV_SAMPLE_FMT_S16,
            16000,
            &input_layout,
            audio_decoder->sample_fmt,
            audio_decoder->sample_rate,
            0,
            NULL
        );
        if (error < 0 || resampler == NULL || swr_init(resampler) < 0) {
            swr_free(&resampler);
            audio_stream_index = -1;
        }
    }

    if (SDL_Init(SDL_INIT_VIDEO | (audio_stream_index >= 0 ? SDL_INIT_AUDIO : 0)) != 0) {
        fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
        goto cleanup;
    }
    if (audio_stream_index >= 0) {
        SDL_AudioSpec desired_audio = {
            .freq = 16000,
            .format = AUDIO_S16SYS,
            .channels = 1,
            .samples = 512,
            .callback = NULL,
        };
        audio_device = SDL_OpenAudioDevice(NULL, 0, &desired_audio, NULL, 0);
        if (audio_device == 0) {
            fprintf(stderr, "SDL audio disabled: %s\n", SDL_GetError());
            swr_free(&resampler);
            avcodec_free_context(&audio_decoder);
            audio_stream_index = -1;
        } else {
            SDL_PauseAudioDevice(audio_device, 0);
        }
    }
    window = SDL_CreateWindow(
        window_title,
        SDL_WINDOWPOS_CENTERED,
        SDL_WINDOWPOS_CENTERED,
        decoder->width > 0 ? decoder->width : 640,
        decoder->height > 0 ? decoder->height : 360,
        SDL_WINDOW_RESIZABLE | SDL_WINDOW_ALLOW_HIGHDPI | SDL_WINDOW_SHOWN
    );
    if (window == NULL) {
        fprintf(stderr, "SDL_CreateWindow: %s\n", SDL_GetError());
        goto cleanup;
    }
    SDL_RaiseWindow(window);
    SDL_SetWindowInputFocus(window);
    renderer = SDL_CreateRenderer(
        window,
        -1,
        SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC
    );
    if (renderer == NULL) {
        renderer = SDL_CreateRenderer(window, -1, SDL_RENDERER_SOFTWARE);
    }
    if (renderer == NULL) {
        fprintf(stderr, "SDL_CreateRenderer: %s\n", SDL_GetError());
        goto cleanup;
    }

    packet = av_packet_alloc();
    decoded = av_frame_alloc();
    display_frame = av_frame_alloc();
    if (packet == NULL || decoded == NULL || display_frame == NULL) {
        fprintf(stderr, "Could not allocate FFmpeg frames or packet.\n");
        goto cleanup;
    }

    fprintf(
        stdout,
        "Native FFmpeg player ready: codec=%s, resolution=%dx%d\n",
        codec->name,
        decoder->width,
        decoder->height
    );
    if (audio_stream_index >= 0) {
        fprintf(stdout, "Native FFmpeg audio: enabled at 16000 Hz mono\n");
    }
    fflush(stdout);

    for (;;) {
        if (should_quit()) {
            result = 0;
            break;
        }
        error = av_read_frame(format, packet);
        if (error == AVERROR_EOF) {
            result = 0;
            break;
        }
        if (error < 0) {
            print_ffmpeg_error("av_read_frame", error);
            goto cleanup;
        }
        if (packet->stream_index == audio_stream_index && audio_decoder != NULL) {
            error = avcodec_send_packet(audio_decoder, packet);
            av_packet_unref(packet);
            if (error < 0 && error != AVERROR(EAGAIN)) {
                print_ffmpeg_error("avcodec_send_packet(audio)", error);
                goto cleanup;
            }
            while ((error = avcodec_receive_frame(audio_decoder, decoded)) >= 0) {
                error = queue_audio_frame(audio_device, resampler, decoded);
                if (error < 0) {
                    print_ffmpeg_error("queue_audio_frame", error);
                    goto cleanup;
                }
            }
            if (error != AVERROR(EAGAIN) && error != AVERROR_EOF) {
                print_ffmpeg_error("avcodec_receive_frame(audio)", error);
                goto cleanup;
            }
            continue;
        }
        if (packet->stream_index != video_stream_index) {
            av_packet_unref(packet);
            continue;
        }

        error = avcodec_send_packet(decoder, packet);
        av_packet_unref(packet);
        if (error < 0 && error != AVERROR(EAGAIN)) {
            print_ffmpeg_error("avcodec_send_packet", error);
            goto cleanup;
        }
        while ((error = avcodec_receive_frame(decoder, decoded)) >= 0) {
            if (scaler == NULL || display_frame->width != decoder->width ||
                display_frame->height != decoder->height ||
                display_frame->format != AV_PIX_FMT_YUV420P) {
                if (scaler != NULL) {
                    sws_freeContext(scaler);
                }
                scaler = sws_getContext(
                    decoder->width,
                    decoder->height,
                    decoder->pix_fmt,
                    decoder->width,
                    decoder->height,
                    AV_PIX_FMT_YUV420P,
                    SWS_FAST_BILINEAR,
                    NULL,
                    NULL,
                    NULL
                );
                if (scaler == NULL) {
                    fprintf(stderr, "Could not create the pixel converter.\n");
                    goto cleanup;
                }
                if (texture != NULL) {
                    SDL_DestroyTexture(texture);
                }
                texture = SDL_CreateTexture(
                    renderer,
                    SDL_PIXELFORMAT_IYUV,
                    SDL_TEXTUREACCESS_STREAMING,
                    decoder->width,
                    decoder->height
                );
                if (texture == NULL) {
                    fprintf(stderr, "SDL_CreateTexture: %s\n", SDL_GetError());
                    goto cleanup;
                }
                if (display_buffer != NULL) {
                    av_freep(&display_buffer);
                }
                display_buffer_size = av_image_alloc(
                    display_frame->data,
                    display_frame->linesize,
                    decoder->width,
                    decoder->height,
                    AV_PIX_FMT_YUV420P,
                    1
                );
                if (display_buffer_size < 0) {
                    print_ffmpeg_error("av_image_alloc", display_buffer_size);
                    goto cleanup;
                }
                display_buffer = display_frame->data[0];
                display_frame->width = decoder->width;
                display_frame->height = decoder->height;
                display_frame->format = AV_PIX_FMT_YUV420P;
            }
            sws_scale(
                scaler,
                (const uint8_t *const *)decoded->data,
                decoded->linesize,
                0,
                decoder->height,
                display_frame->data,
                display_frame->linesize
            );
            render_frame(renderer, texture, display_frame, decoder->width, decoder->height);
        }
        if (error != AVERROR(EAGAIN) && error != AVERROR_EOF) {
            print_ffmpeg_error("avcodec_receive_frame", error);
            goto cleanup;
        }
    }

cleanup:
    if (display_buffer != NULL) {
        av_freep(&display_buffer);
    }
    if (texture != NULL) {
        SDL_DestroyTexture(texture);
    }
    if (renderer != NULL) {
        SDL_DestroyRenderer(renderer);
    }
    if (window != NULL) {
        SDL_DestroyWindow(window);
    }
    if (audio_device != 0) {
        SDL_CloseAudioDevice(audio_device);
    }
    SDL_Quit();
    sws_freeContext(scaler);
    swr_free(&resampler);
    av_frame_free(&display_frame);
    av_frame_free(&decoded);
    av_packet_free(&packet);
    avcodec_free_context(&audio_decoder);
    avcodec_free_context(&decoder);
    avformat_close_input(&format);
    return result;
}
